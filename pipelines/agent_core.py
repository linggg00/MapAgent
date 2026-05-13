from __future__ import annotations

import json
import os
import re
from typing import Any

from schemas.dag_model import ClarificationSlot, POICandidate, ReflectionRound, RouteClarification, RoutePlanResponse
from schemas.request import RoutePlanRequest
from services.amap_client import AmapClient
from services.llm_client import LLMClient
from services.reward_policy import RewardPolicyService
from services.validator import Validator

class PDRAgentCore:
    def __init__(
        self,
        amap_client: AmapClient | None = None,
        llm_client: LLMClient | None = None,
        validator: Validator | None = None,
        reward_policy: RewardPolicyService | None = None,
    ) -> None:
        self.amap_client = amap_client or AmapClient()
        self.llm_client = llm_client or LLMClient()
        self.validator = validator or Validator()
        self.reward_policy = reward_policy or RewardPolicyService()

        # =========================================================
        # 🧠 泛化图级别 RL (Graph-Level RL) 配置
        # =========================================================
        self.q_table_path = "runtime/dag_actions_q_table.json"
        self.learning_rate = 0.5  # Q-Learning 学习率
        self._ensure_q_table_exists()

    def _ensure_q_table_exists(self):
        os.makedirs("runtime", exist_ok=True)
        if not os.path.exists(self.q_table_path):
            self._save_q_table({"actions_q_values": {}})

    def _load_q_table(self) -> dict:
        try:
            with open(self.q_table_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"actions_q_values": {}}

    def _save_q_table(self, q_table: dict):
        with open(self.q_table_path, "w", encoding="utf-8") as f:
            json.dump(q_table, f, indent=2)

    def _resolve_params(self, params: dict, context_vars: dict) -> dict:
        """【黄金十条适配版】动态 DAG 上下文参数替换"""
        resolved = {}
        import re # 确保顶部或这里导入了 re
        
        for key, value in params.items():
            # 1. 如果是字符串形式的变量引用
            if isinstance(value, str):
                # 【🛡️ 终极防弹衣：自动修复大模型漏掉 $ 的情况】
                # 如果发现字符串长得像 "T1.result" 或 "T2.result"，强制给它加上 $
                if re.match(r'^T\d+\.result', value):
                    print(f"[warn] [系统自动纠错] 检测到大模型遗漏 '$' 符号，已自动修复: {value} -> ${value}")
                    value = "$" + value
                    
                if value.startswith("$"):
                    ref_task_id = value[1:].split(".")[0] 
                    resolved[key] = context_vars.get(ref_task_id, value)
                elif value in context_vars: 
                    resolved[key] = context_vars[value]
                elif value in ["current_location", "route_nearby", "当前位置"]:
                    resolved[key] = context_vars.get("current_gps", value)
                else:
                    resolved[key] = value
                    
            # 2. 如果是数组形式的变量引用 (如训练集里的 "waypoints": ["$T2.result"])
            elif isinstance(value, list):
                resolved_list = []
                for item in value:
                    if isinstance(item, str) and item.startswith("$"):
                        ref_task_id = item[1:].split(".")[0]
                        resolved_item = context_vars.get(ref_task_id, item)
                        if isinstance(resolved_item, str) and resolved_item: # 确保提取到真实坐标
                            resolved_list.append(resolved_item)
                    else:
                        resolved_list.append(item)
                
                # 【高德API适配】：如果键名是 waypoints，高德要求用分号拼接多个坐标
                if key == "waypoints" and resolved_list:
                    resolved[key] = ";".join([str(x) for x in resolved_list if isinstance(x, str)])
                else:
                    resolved[key] = resolved_list
            else:
                resolved[key] = value
                
        return resolved

    def _is_default_placeholder(self, value: str | None) -> bool:
        return not value or value in {"默认起点", "默认终点"}

    def _simplify_keyword(self, keyword: str) -> str:
        cleaned = re.sub(r"^(.*?省|.*?市|.*?区|.*?县)", "", (keyword or "").strip())
        cleaned = re.sub(r"[，,。！？!?；;]", "", cleaned)
        cleaned = re.sub(r"评分(?:在)?\s*\d(?:\.\d)?\s*以上的?", "", cleaned).strip()
        cleaned = re.sub(r"(评分最高的?|高评分的?|评分较高的?)", "", cleaned).strip()
        cleaned = re.sub(r"(怎么走|如何走|怎么去|如何去|导航|路线|看病|就诊|挂号|看医生|看病去)$", "", cleaned).strip()
        if re.search(r"(医院|卫生院|社康|社区健康服务中心|诊所)", cleaned):
            cleaned = re.sub(r"(看病|就诊|挂号|看医生|看病去)", "", cleaned).strip()
        prefix_pattern = r"^(离我最近的|我附近的|附近最近的|附近的|周边的|就近的|最近的|附近|周边|就近|最近|我附近)"
        suffix_pattern = r"(最近的|附近|周边|最近)$"
        previous = None
        while previous != cleaned:
            previous = cleaned
            cleaned = re.sub(prefix_pattern, "", cleaned).strip()
            cleaned = re.sub(suffix_pattern, "", cleaned).strip()
        if re.fullmatch(r"(医院|卫生院|社康|社区健康服务中心|诊所).+", cleaned) and any(
            token in cleaned for token in ["医院", "卫生院", "社康", "诊所"]
        ):
            for token in ["医院", "卫生院", "社康", "诊所"]:
                if token in cleaned:
                    cleaned = token
                    break
        return cleaned.strip("，,。.!！？ ")

    def _extract_poi_constraints(self, text: str) -> dict[str, Any]:
        constraints: dict[str, Any] = {}
        rating_match = re.search(r"评分(?:在)?\s*(\d(?:\.\d)?)\s*以上", text or "")
        if rating_match:
            constraints["min_rating"] = float(rating_match.group(1))
        if any(token in (text or "") for token in ["评分最高", "评分高", "高评分", "口碑最好", "评价最好"]):
            constraints["sort"] = "rating"
        elif any(token in (text or "") for token in ["最近", "就近", "附近", "离我近"]):
            constraints["sort"] = "distance"
        return constraints

    def _extract_route_entities(self, text: str) -> tuple[str, list[str]]:
        if not text:
            return "", []

        normalized = re.sub(r"[。！？!?；;]", "，", text).strip()

        waypoint_keywords: list[str] = []
        waypoint_patterns = [
            r"(?:途经|途径|经过|路过)([^，]+)",
            r"(?:顺路|顺便)(?:去|到|经过|途经|途径|要途经|要途径)?([^，]+)",
        ]
        for pattern in waypoint_patterns:
            for match in re.finditer(pattern, normalized):
                raw_segment = match.group(1)
                for piece in re.split(r"[、,，和及]", raw_segment):
                    keyword = self._simplify_keyword(piece)
                    if keyword and keyword not in waypoint_keywords:
                        waypoint_keywords.append(keyword)

        destination = ""
        destination_patterns = [
            r"(?:我想去|我要去|想去|去|到|前往|导航到|导航去|带我去)([^，]+?)(?:，|$)",
            r"(?:我想要|我要|想要|帮我找|帮我去)([^，]+?)(?:，|$)",
        ]
        for pattern in destination_patterns:
            match = re.search(pattern, normalized)
            if not match:
                continue
            candidate = self._simplify_keyword(match.group(1))
            for prefix in ["途经", "途径", "经过", "路过", "顺路", "顺便"]:
                if candidate.startswith(prefix):
                    candidate = ""
                    break
            if candidate:
                destination = candidate
                break

        if destination:
            destination = re.sub(r"^(一下|一个|一家)", "", destination).strip()
            destination = re.sub(r"(附近的|周边的)$", "", destination).strip()

        return destination, waypoint_keywords

    def _prepare_request_from_query(self, request: RoutePlanRequest) -> RoutePlanRequest:
        prepared = request.model_copy(deep=True)
        source_text = prepared.query or prepared.user_request
        destination, waypoint_keywords = self._extract_route_entities(source_text)

        if self._is_default_placeholder(prepared.destination) and destination:
            prepared.destination = destination

        if not prepared.extra.get("waypoints") and waypoint_keywords:
            prepared.extra["waypoints"] = waypoint_keywords

        prepared.extra["poi_constraints"] = self._extract_poi_constraints(source_text)

        if not prepared.user_request and prepared.query:
            prepared.user_request = prepared.query
        return prepared

    def _extract_waypoint_keywords(self, request: RoutePlanRequest) -> list[str]:
        raw_waypoints = request.extra.get("waypoints")
        if isinstance(raw_waypoints, str):
            return [item.strip() for item in re.split(r"[、,，]", raw_waypoints) if item.strip()]
        if isinstance(raw_waypoints, list):
            return [str(item).strip() for item in raw_waypoints if str(item).strip()]
        return []

    def _resolve_search_center(self, request: RoutePlanRequest) -> str:
        current_gps = str(request.extra.get("current_gps", "")).strip()
        if current_gps:
            return current_gps
        if request.start and not self._is_default_placeholder(request.start):
            if self.amap_client._is_coord(request.start):
                return request.start
        return ""

    def _looks_ambiguous(self, keyword: str, candidates: list[dict[str, Any]]) -> bool:
        if len(candidates) < 2:
            return False

        simplified = self._simplify_keyword(keyword)
        if not simplified:
            return False

        generic_tokens = {
            "麦当劳", "肯德基", "公共厕所", "厕所", "卫生间", "洗手间", "加油站", "充电站",
            "停车场", "医院", "药店", "咖啡店", "咖啡馆", "便利店", "餐厅", "饭店", "酒店",
            "宾馆", "超市", "商场", "公园", "地铁站", "服务区", "银行", "奶茶店",
            "烤肉店", "火锅店", "烧烤店", "面馆", "快餐店",
        }
        generic_suffixes = (
            "店", "馆", "场", "站", "酒店", "宾馆", "公园", "医院", "餐厅",
            "饭店", "厕所", "卫生间", "洗手间", "加油站", "充电站", "咖啡馆",
            "咖啡店", "烤肉店", "火锅店", "烧烤店", "超市",
        )
        specific_markers = re.search(r"\d|路|街|号|广场|大厦|小区|门|分店|校区|航站楼|服务中心", simplified)
        nearby_matches = 0
        for candidate in candidates[:3]:
            name = candidate.get("name", "")
            address = candidate.get("address", "")
            if simplified in name or simplified in address:
                nearby_matches += 1

        is_generic_phrase = simplified in generic_tokens or any(
            simplified.endswith(suffix) for suffix in generic_suffixes
        )
        return (
            is_generic_phrase and not specific_markers
        ) or (
            nearby_matches >= 2 and not specific_markers and len(simplified) <= 6
        )

    def _repair_route_endpoint_from_waypoints(
        self,
        start_coord: Any,
        end_coord: Any,
        waypoints: Any,
        fallback_end: str = "",
        current_gps: str = "",
    ) -> tuple[str, str, str]:
        normalized_start = start_coord if isinstance(start_coord, str) else current_gps
        normalized_end = end_coord if isinstance(end_coord, str) else ""
        normalized_waypoints = waypoints if isinstance(waypoints, str) else ""

        waypoint_items = [item.strip() for item in normalized_waypoints.split(";") if item.strip()]
        if not normalized_end and waypoint_items:
            if len(waypoint_items) >= 2:
                normalized_end = waypoint_items[-1]
                normalized_waypoints = ";".join(waypoint_items[:-1])
            elif fallback_end:
                normalized_end = fallback_end

        return normalized_start, normalized_end, normalized_waypoints

    def _resolve_candidate_selection(
        self,
        slot_id: str,
        keyword: str,
        candidates: list[dict[str, Any]],
        selections: dict[str, Any],
    ) -> dict[str, Any] | None:
        raw_selection = selections.get(slot_id)
        if raw_selection is None:
            raw_selection = selections.get(keyword)
        if raw_selection is None:
            return None

        if isinstance(raw_selection, dict):
            for matcher in ("location", "id", "name", "index"):
                if raw_selection.get(matcher) is not None:
                    raw_selection = raw_selection.get(matcher)
                    break

        if isinstance(raw_selection, int) or (isinstance(raw_selection, str) and raw_selection.isdigit()):
            index = int(raw_selection)
            if 1 <= index <= len(candidates):
                return candidates[index - 1]
            if 0 <= index < len(candidates):
                return candidates[index]

        for candidate in candidates:
            if raw_selection in {candidate.get("location"), candidate.get("id"), candidate.get("name")}:
                return candidate
        return None

    def _build_pending_slot(
        self,
        slot_id: str,
        slot_type: str,
        keyword: str,
        candidates: list[dict[str, Any]],
    ) -> ClarificationSlot:
        question = f"您想选哪一个{keyword}？" if slot_type == "destination" else f"您想途经哪一个{keyword}？"
        return ClarificationSlot(
            slot_id=slot_id,
            slot_type=slot_type,
            keyword=keyword,
            question=question,
            candidates=[POICandidate(**candidate) for candidate in candidates],
        )

    def _build_clarification_prompt(self, slots: list[ClarificationSlot]) -> str:
        questions = [slot.question for slot in slots]
        return "；".join(questions)

    def _resolve_request_clarification(
        self,
        request: RoutePlanRequest,
    ) -> tuple[RoutePlanRequest, RouteClarification | None]:
        prepared = self._prepare_request_from_query(request)
        center_coord = self._resolve_search_center(prepared)
        if not center_coord:
            return prepared, None

        selections = prepared.extra.get("poi_selections", {})
        if not isinstance(selections, dict):
            selections = {}

        resolved_slots: dict[str, POICandidate] = {}
        pending_slots: list[ClarificationSlot] = []

        destination_keyword = "" if self._is_default_placeholder(prepared.destination) else prepared.destination
        destination_coord = ""
        source_text = prepared.query or prepared.user_request or ""
        poi_constraints = prepared.extra.get("poi_constraints") or {}
        poi_sort = "rating" if poi_constraints.get("sort") == "rating" else "distance"
        min_rating = poi_constraints.get("min_rating")
        wants_nearest = any(token in source_text for token in ["最近", "就近", "附近", "离我近"])
        wants_ranked = poi_constraints.get("sort") == "rating" or min_rating is not None
        search_radius = 5000 if wants_nearest else 50000
        is_medical_destination = any(
            token in destination_keyword
            for token in ["医院", "卫生院", "社康", "社区健康服务中心", "诊所"]
        )

        if destination_keyword:
            destination_candidates = self.amap_client.search_around_poi_candidates(
                center_coord,
                destination_keyword,
                poi_sort,
                limit=5,
                radius=search_radius,
                min_rating=min_rating,
            )
            selected_destination = self._resolve_candidate_selection(
                "destination", destination_keyword, destination_candidates, selections
            )
            if selected_destination:
                prepared.extra["resolved_destination"] = selected_destination
                prepared.extra["resolved_destination_coord"] = selected_destination.get("location")
                resolved_slots["destination"] = POICandidate(**selected_destination)
                destination_coord = selected_destination.get("location", "")
            elif (wants_ranked or (wants_nearest and is_medical_destination)) and destination_candidates:
                prepared.extra["resolved_destination"] = destination_candidates[0]
                prepared.extra["resolved_destination_coord"] = destination_candidates[0].get("location")
                destination_coord = destination_candidates[0].get("location", "")
            elif self._looks_ambiguous(destination_keyword, destination_candidates):
                pending_slots.append(
                    self._build_pending_slot("destination", "destination", destination_keyword, destination_candidates)
                )
                return prepared, RouteClarification(
                    prompt=self._build_clarification_prompt(pending_slots),
                    pending_slots=pending_slots,
                    resolved_slots=resolved_slots,
                )
            elif destination_candidates:
                prepared.extra["resolved_destination"] = destination_candidates[0]
                prepared.extra["resolved_destination_coord"] = destination_candidates[0].get("location")
                destination_coord = destination_candidates[0].get("location", "")

        waypoint_keywords = self._extract_waypoint_keywords(prepared)
        resolved_waypoints: list[dict[str, Any]] = []
        for index, keyword in enumerate(waypoint_keywords, start=1):
            slot_id = f"waypoint_{index}"
            waypoint_candidates: list[dict[str, Any]] = []
            if center_coord and destination_coord:
                waypoint_candidates = self.amap_client.search_along_route_candidates(
                    center_coord, destination_coord, keyword, limit=5
                )
            if not waypoint_candidates:
                waypoint_candidates = self.amap_client.search_around_poi_candidates(
                    center_coord, keyword, "distance", limit=5
                )

            selected_waypoint = self._resolve_candidate_selection(slot_id, keyword, waypoint_candidates, selections)
            if selected_waypoint:
                resolved_waypoints.append(selected_waypoint)
                resolved_slots[slot_id] = POICandidate(**selected_waypoint)
            elif self._looks_ambiguous(keyword, waypoint_candidates):
                pending_slots.append(self._build_pending_slot(slot_id, "waypoint", keyword, waypoint_candidates))
            elif waypoint_candidates:
                resolved_waypoints.append(waypoint_candidates[0])

        if resolved_waypoints:
            prepared.extra["resolved_waypoints"] = resolved_waypoints

        if pending_slots:
            return prepared, RouteClarification(
                prompt=self._build_clarification_prompt(pending_slots),
                pending_slots=pending_slots,
                resolved_slots=resolved_slots,
            )
        return prepared, None

    # =========================================================
    # 👑 泛化信用分配算法 (Generalized Credit Assignment)
    # =========================================================
    def _calculate_generalized_reward(self, execution_trace: list, final_report: dict | None) -> float:
        """根据整个 DAG 的执行轨迹，给出一个最终的全局环境反馈分数"""
        if not execution_trace: 
            return -50.0  # 连动作都没生成，极差

        # 检查是否所有节点都成功了
        failed_nodes = [node for node in execution_trace if node["status"] != "success"]
        
        if not failed_nodes and final_report:
            return 20.0   # 完美通关，奖励 20 分
        else:
            # 每失败一个节点，扣 10 分
            return -10.0 * len(failed_nodes)

    def _assign_credit_and_update_q(self, execution_trace: list, final_reward: float):
        """
        【真正的泛化信用分配】：无论大模型调用了什么工具，
        根据最终结果，利用 TD(0) 思想反向更新各个工具动作的 Q 值。
        """
        q_table = self._load_q_table()
        actions_q = q_table.setdefault("actions_q_values", {})

        print("\n" + "="*50)
        print(f"[rl] [泛化信用分配] DAG全局最终得分: {final_reward}")
        
        for trace in execution_trace:
            action = trace["action"]
            status = trace["status"]

            if action not in actions_q:
                actions_q[action] = 0.0  # 初始化新见到的动作

            # 信用分配核心逻辑：找罪魁祸首！
            node_reward = final_reward
            if final_reward < 0:
                if status != "success":
                    # 导致整条链崩溃的直接责任人，承担 1.5 倍的惩罚！
                    node_reward = final_reward * 1.5
                    print(f"   [fail] 节点 '{action}' 执行失败，承担主要惩罚。")
                else:
                    # 队友坑了，自己其实成功了，轻微连带受罚
                    node_reward = final_reward * 0.2
                    print(f"   [warn] [连带] 节点 '{action}' 自身成功，受轻微连带惩罚。")

            # 经典的 Q-Learning 更新公式
            old_q = actions_q[action]
            new_q = old_q + self.learning_rate * (node_reward - old_q)
            actions_q[action] = round(new_q, 4)

            print(f"   [rl] 更新 Q 值: [{action}] {old_q:.2f} -> {new_q:.2f}")

        print("="*50 + "\n")
        self._save_q_table(q_table)

    def _build_route_report_from_resolved_pois(
        self,
        request: RoutePlanRequest,
        current_gps: str,
        strategy: str = "speed",
    ) -> dict:
        resolved_destination = request.extra.get("resolved_destination")
        resolved_waypoints = request.extra.get("resolved_waypoints", [])

        destination_coord = ""
        pois: list[dict[str, Any]] = []
        if isinstance(resolved_destination, dict):
            destination_coord = resolved_destination.get("location", "")
            pois.append({"role": "destination", **resolved_destination})

        waypoint_coords: list[str] = []
        if isinstance(resolved_waypoints, list):
            for waypoint in resolved_waypoints:
                if isinstance(waypoint, dict) and waypoint.get("location"):
                    waypoint_coords.append(waypoint["location"])
                    pois.append({"role": "waypoint", **waypoint})

        if not destination_coord and not self._is_default_placeholder(request.destination):
            destination_coord = self.amap_client._ensure_coord(request.destination)

        if not destination_coord:
            return {"status": "failed", "reason": "未解析到目的地坐标"}

        route_data = self.amap_client.get_driving_route_by_coords(
            origin=current_gps,
            dest=destination_coord,
            waypoints=";".join(waypoint_coords),
            strategy=strategy,
        )
        if not route_data:
            return {"status": "failed", "reason": "路线规划接口未返回有效结果"}

        route_data["origin"] = current_gps
        route_data["destination"] = destination_coord
        if isinstance(resolved_destination, dict):
            route_data["destination_name"] = resolved_destination.get("name") or ""
            route_data["destination_address"] = resolved_destination.get("address") or ""
            route_data["destination_rating"] = resolved_destination.get("rating")
        route_data["waypoints"] = waypoint_coords
        route_data["pois"] = pois
        route_data["fallback"] = "resolved_poi_route"
        return route_data

    # =========================================================
    # 🌟 终极动态 DAG 引擎
    # =========================================================
    def execute_dag_engine(self, user_query: str, current_gps: str, poi_selections: dict[str, Any] | None = None) -> dict:
        print(f"\n[agent] 正在思考如何拆解任务: '{user_query}'...")
        clarification_request = RoutePlanRequest(
            query=user_query,
            user_request=user_query,
            start=current_gps,
            destination="默认终点",
            need_narration=False,
            use_mock_data=False,
            extra={"current_gps": current_gps, "poi_selections": poi_selections or {}},
        )
        clarification_request, clarification = self._resolve_request_clarification(clarification_request)
        if clarification:
            return {"status": "needs_clarification", "clarification": clarification.model_dump()}

        forced_poi_lookup: dict[str, dict[str, Any]] = {}
        resolved_pois: list[dict[str, Any]] = []
        resolved_destination = clarification_request.extra.get("resolved_destination")
        if isinstance(resolved_destination, dict):
            destination_keyword = self._simplify_keyword(clarification_request.destination)
            if destination_keyword:
                forced_poi_lookup[destination_keyword] = resolved_destination
            resolved_pois.append({"role": "destination", **resolved_destination})
        for keyword, waypoint in zip(
            self._extract_waypoint_keywords(clarification_request),
            clarification_request.extra.get("resolved_waypoints", []),
        ):
            if isinstance(waypoint, dict):
                forced_poi_lookup[self._simplify_keyword(keyword)] = waypoint
                resolved_pois.append({"role": "waypoint", **waypoint})

         # 🌟 每次规划前，先读取历史 Q 表，传给大模型更新策略
        q_table_data = self._load_q_table()
        actions_q = q_table_data.get("actions_q_values", {})

        try:
            # 调用真实的大模型意图解析
            raw_llm_output = self.llm_client.parse_user_intent_to_dag(user_query, q_table=actions_q)
            print(f"\n[llm raw output]\n{raw_llm_output}\n" + "=" * 50)
            llm_generated_dag = {}
            if isinstance(raw_llm_output, str):
                import re, json
                json_match = re.search(r'\{.*\}', raw_llm_output, re.DOTALL)
                if json_match:
                    llm_generated_dag = json.loads(json_match.group(0))
                else:
                    raise ValueError("未找到合法 JSON")
            elif isinstance(raw_llm_output, dict):
                llm_generated_dag = raw_llm_output
                
        except Exception as e:
            print(f"[warn] 语义解析崩溃: {e}")
            fallback_report = self._build_route_report_from_resolved_pois(clarification_request, current_gps)
            if isinstance(fallback_report, dict):
                fallback_report["dag_source"] = "fallback"
                fallback_report["dag_error"] = str(e)
            return fallback_report
            
        if not llm_generated_dag or "tasks" not in llm_generated_dag:
            fallback_report = self._build_route_report_from_resolved_pois(clarification_request, current_gps)
            if isinstance(fallback_report, dict):
                fallback_report["dag_source"] = "fallback"
                fallback_report["dag_error"] = "模型未返回有效 DAG tasks"
            return fallback_report

        print("[agent] 成功生成泛化 DAG 任务流:")
        for t in llm_generated_dag["tasks"]:
            print(f"   -> {t.get('task_id')}: 执行 {t.get('action')} (动态参数: {t.get('params')})")

        context_vars = {"current_gps": current_gps}
        final_route_report = None
        fetched_weather = None 
        # 【新增】：轨迹记录器，用于信用分配
        execution_trace = [] 

        print("\n[executor] 开始动态执行工具链...")
        for task in llm_generated_dag["tasks"]:
            raw_action = str(task.get("action") or "").strip()
            action = {
                "poi_search": "search_poi",
                "search_place": "search_poi",
                "search_destination": "search_poi",
                "route_plan": "plan_route",
                "route_planning": "plan_route",
                "drive_route": "plan_route",
                "get_weather_info": "get_weather",
                "check_weather": "get_weather",
                "weather": "get_weather",
            }.get(raw_action, raw_action)
            raw_params = task.get("params", {})
            task_id = task.get("task_id")

            params = self._resolve_params(raw_params, context_vars)
            
            # 初始化该节点的轨迹记录
            trace_record = {"task_id": task_id, "action": action, "status": "pending"}

            # --- 泛化路由网关 ---
          # --- 黄金十条统一路由网关 ---
            try:
                if action == "search_poi":
                    # 1. 提取基础参数
                    center = params.get("location", current_gps)
                    poi_keyword = params.get("poi_type", "目标地点")
                    
                    # ==================================================
                    # 🛡️ 终极防呆兜底：兼容大模型自行发明的 poi_name, keyword 等
                    # ==================================================
                    poi_name = params.get("poi_name", "") or params.get("name", "") or params.get("keyword", "")
                    if poi_name and poi_name not in poi_keyword:
                        # 把 "越秀公园" 和 "公园" 拼起来 -> "越秀公园 公园"
                        poi_keyword = f"{poi_name} {poi_keyword}".strip()
                    
                    # 2. 将训练集里的 attributes (如["允许带宠物"]) 拼接到搜索词里
                    attributes = params.get("attributes", [])
                    if isinstance(attributes, list) and attributes:
                        poi_keyword = f"{poi_keyword} " + " ".join(attributes)
                    
                    # 3. 根据大模型的意图动态选择排序方式（如果模型指定了 distance 就用距离，否则默认权重）
                    sortrule = "distance" if params.get("sort") == "distance" else "weight"

                    forced_candidate = None
                    simplified_keyword = self._simplify_keyword(poi_keyword)
                    if simplified_keyword:
                        forced_candidate = forced_poi_lookup.get(simplified_keyword)

                    if forced_candidate:
                        result_coord = forced_candidate.get("location", "")
                    else:
                        # 4. 执行底层 API 动作
                        result_coord = self.amap_client.search_around_poi(center, poi_keyword, sortrule)
                    context_vars[task_id] = result_coord
                    
                    trace_record["status"] = "success" if result_coord else "empty_result"

                elif action == "plan_route":
                    start_coord = params.get("start") or params.get("origin") or current_gps
                    end_coord = params.get("end") or params.get("destination")
                    waypoints = params.get("waypoints", "")
                    if isinstance(waypoints, list):
                        waypoints = ";".join(str(item) for item in waypoints if str(item).strip())
                    resolved_waypoint_coords = []
                    for resolved_waypoint in clarification_request.extra.get("resolved_waypoints", []):
                        if isinstance(resolved_waypoint, dict) and resolved_waypoint.get("location"):
                            resolved_waypoint_coords.append(resolved_waypoint["location"])
                    if resolved_waypoint_coords:
                        existing_waypoints = [item for item in str(waypoints).split(";") if item.strip()]
                        for coord in resolved_waypoint_coords:
                            if coord not in existing_waypoints:
                                existing_waypoints.append(coord)
                        waypoints = ";".join(existing_waypoints)
                    fallback_end = ""
                    if isinstance(clarification_request.extra.get("resolved_destination"), dict):
                        fallback_end = clarification_request.extra["resolved_destination"].get("location", "")
                    
                    # 🛡️ 获取大模型决定的路线策略，默认速度优先
                    route_strategy = params.get("strategy", "speed")

                    start_coord, end_coord, waypoints = self._repair_route_endpoint_from_waypoints(
                        start_coord=start_coord,
                        end_coord=end_coord,
                        waypoints=waypoints,
                        fallback_end=fallback_end,
                        current_gps=current_gps,
                    )
                    
                    if start_coord == end_coord:
                        print("[warn] 起终点相同，已将起点重置为 current_gps")
                        start_coord = current_gps
                    if not start_coord or start_coord == "current_gps":
                        start_coord = current_gps

                    if hasattr(self.amap_client, "get_driving_route_by_coords") and start_coord and end_coord:
                        # 传入 route_strategy
                        route_data = self.amap_client.get_driving_route_by_coords(start_coord, end_coord, waypoints, route_strategy)
                        if route_data:
                            route_data["origin"] = start_coord
                            route_data["destination"] = end_coord
                            route_data["waypoints"] = [item for item in waypoints.split(";") if item]
                            route_data["pois"] = resolved_pois
                            if isinstance(clarification_request.extra.get("resolved_destination"), dict):
                                resolved_destination_meta = clarification_request.extra["resolved_destination"]
                                route_data["destination_name"] = resolved_destination_meta.get("name") or ""
                                route_data["destination_address"] = resolved_destination_meta.get("address") or ""
                                route_data["destination_rating"] = resolved_destination_meta.get("rating")
                        context_vars[task_id] = route_data
                        final_route_report = route_data
                        trace_record["status"] = "success" if route_data.get("distance_km") else "api_failed"
                    else:
                        trace_record["status"] = "missing_parameters"
                        # ... 前面的 search_poi 和 plan_route 代码 ...

                # ==================================================
                # 🛠️ 新增工具分支：天气查询
                # ==================================================
             # ==================================================
                # 🛠️ 新增工具分支：天气查询
                # ==================================================
                # ==================================================
                # 🛠️ 新增工具分支：天气查询
                # ==================================================
                elif action == "get_weather":
                    # 1. 兼容大模型的字段发明症：同时支持 location 和 city
                    location_name = params.get("location") or params.get("city")
                    
                    # 2. 🛡️ 拦截非法参数：如果大模型没给参数，或者传入了经纬度坐标
                    if not location_name or location_name == "current_gps" or "," in location_name:
                        print(f"[warn] 大模型未指定明确地名或传入了坐标 '{location_name}'，尝试智能兜底")
                        location_name = "深圳市" 
                        
                    # 3. 执行安全的查询
                    weather_res = self.amap_client.get_weather_by_location(location_name)
                    context_vars[task_id] = weather_res
                    fetched_weather = weather_res 
                    trace_record["status"] = "success" if weather_res != "未知" else "api_failed"
                # 都不需要改信用分配算法，直接在这里加 if-elif 即可！
                else:
                    print(f"[warn] 遇到未知的动作节点: {action}")
                    trace_record["status"] = "unknown_action"
                    
            except Exception as e:
                print(f"[error] 执行 {action} 时发生异常: {e}")
                trace_record["status"] = "exception"

            # 将本节点执行结果记入历史轨迹
            execution_trace.append(trace_record)
            
            # 如果某个节点直接报异常了，后续的依赖图往往会崩溃，可以选择直接 break
            if trace_record["status"] not in ["success"]:
                print(f"[stop] 任务链中断，节点 {task_id} 执行失败")
                break

        # =========================================================
        # 🎓 核心：执行完毕，调用泛化强化学习进行信用分配！
        # =========================================================
        final_reward = self._calculate_generalized_reward(execution_trace, final_route_report)
        self._assign_credit_and_update_q(execution_trace, final_reward)
        if isinstance(final_route_report, dict) and fetched_weather:
            final_route_report["weather"] = fetched_weather
        if isinstance(final_route_report, dict) and final_route_report.get("distance_km") is not None:
            final_route_report["dag_source"] = "llm"
            return final_route_report

        fallback_report = self._build_route_report_from_resolved_pois(clarification_request, current_gps)
        if fallback_report.get("distance_km") is not None:
            fallback_report["dag_source"] = "fallback"
            fallback_report["dag_error"] = "DAG 未生成有效路线，使用已解析 POI 兜底"
            return fallback_report

        return {
            "status": "failed",
            "reason": fallback_report.get("reason", "动态 DAG 未能生成有效路线"),
            "origin": current_gps,
            "query": user_query,
        }

    # ---------------------------------------------------------
    # 模块 4: 兼容原有的反射与校验执行器 (不影响你基于 reward_policy 的旧测试)
    # ---------------------------------------------------------
    def execute(self, request: RoutePlanRequest) -> RoutePlanResponse:
        """
        这里保留了你原有的架构，完美接驳你写的那个牛逼的 reward_policy.py
        """
        prepared_request, clarification = self._resolve_request_clarification(request)
        if clarification:
            return RoutePlanResponse(
                status="needs_clarification",
                request_echo=prepared_request,
                narration=clarification.prompt,
                clarification=clarification,
            )

        request = prepared_request
        environment = self.amap_client.get_environment_snapshot(request)
        context_key = self.reward_policy.derive_context_key(request, environment)
        reflection_context: str | None = None
        reflection_trace: list[ReflectionRound] = []
        learning_trace = []

        final_dag = None
        final_execution = None
        final_validation = None

        for round_index in range(request.max_reflection_rounds + 1):
            dag = self.llm_client.generate_planning_dag(request, environment, reflection_context)
            policy_before = self.reward_policy.snapshot(context_key)
            
            selected_execution = self.amap_client.build_execution_plan(
                request, environment, dag, reflection_context
            )
            
            selected_validation = self.validator.validate(request, dag, selected_execution, environment)

            policy_after = self.reward_policy.snapshot(context_key)
            if policy_after != policy_before:
                learning_trace.append(f"Round {round_index}: Policy updated -> {policy_after}")

            final_dag = dag
            final_execution = selected_execution
            final_validation = selected_validation

            if selected_validation.passed:
                narration = None
                if request.need_narration:
                    narration = self.llm_client.generate_narration(request, environment, selected_execution, selected_validation)
                return RoutePlanResponse(
                    status="success",
                    request_echo=request,
                    environment=environment,
                    dag=dag,
                    execution_plan=selected_execution,
                    validation=selected_validation,
                    narration=narration,
                    reflection_trace=reflection_trace,
                    learning_trace=learning_trace,
                )

            reflection_context = self.validator.build_reflection_context(selected_validation)

        return RoutePlanResponse(
            status="failed",
            request_echo=request,
            environment=environment,
            dag=final_dag,
            execution_plan=final_execution,
            validation=final_validation,
            narration=None,
            reflection_trace=reflection_trace,
            learning_trace=learning_trace,
        )
