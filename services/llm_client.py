from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from schemas.dag_model import EnvironmentSnapshot, ExecutionPlan, PlanningDAG, RoutePlanResponse, RouteSegment, ValidationReport
from schemas.dag_model import DagEdge, DagNode
from schemas.request import RoutePlanRequest


DEFAULT_LLM_BASE_URL = "https://u969042-a058-0cc24867.bjb2.seetacloud.com:8443/v1"


class LLMClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        # 直接配置为你本地模型的默认地址和名称，避免后续再报错
        configured_base_url = base_url if base_url is not None else os.getenv("MAP_AGENT_LLM_BASE_URL", DEFAULT_LLM_BASE_URL)
        self.base_url = configured_base_url.rstrip("/")
        self.api_key = api_key or os.getenv("MAP_AGENT_LLM_API_KEY") or "EMPTY"
        self.model = model or os.getenv("MAP_AGENT_LLM_MODEL") or "llama3_cot_v2" # 使用你之前测试的模型名
        
        template_dir = Path(__file__).resolve().parent.parent / "templates"
        self.planner_template = (template_dir / "planner_cot.txt").read_text(encoding="utf-8")
        self.narrator_template = (template_dir / "narrator.txt").read_text(encoding="utf-8")

    def generate_planning_dag(
        self,
        request: RoutePlanRequest,
        environment: EnvironmentSnapshot,
        reflection_context: str | None = None,
    ) -> PlanningDAG:
        
        # 【修改点 1】严格对齐你训练集里的 Instruction 和 Input
        if reflection_context and reflection_context != "无":
            # 如果是反思阶段，使用反思代理的提示词
            system_instruction = "你是一个地图导航系统的【反思代理】。上一步的工具调用失败了，请分析错误信息，并给出修正后的下一步行动方案。"
            user_input = f"用户原始需求：{request.query or request.user_request}\n上一轮校验失败原因：{reflection_context}\n请修正并重新输出 JSON。"
        else:
            # 正常规划阶段，完美复刻你训练集里的提示词
            system_instruction = "你是一个地图导航系统的【规划代理】。请分析用户的复杂出行指令，并将其分解为带有依赖关系的 DAG（有向无环图）任务序列。输出需严格遵循 JSON 格式。"
            # 直接传入带有“天河软件园...”等字样的自然语言原话
            user_input = request.query or request.user_request

        # 组装为标准的对话消息数组
        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_input}
        ]

        if self._can_call_live():
            try:
                # 温度设低一点，保证输出 JSON 的稳定性
                response_text = self._chat(messages, temperature=0.1)
                dag = self._parse_dag_from_text(response_text)
                if dag:
                    return dag
            except Exception as e:
                print(f"[warn] 调用本地大模型出现异常: {e}")
                pass
        
        # 如果模型抽风了，使用兜底机制
        return self._fallback_dag(request, environment, reflection_context)
    def parse_user_intent_to_dag(self, user_query: str, q_table: dict = None) -> str:
        """
        【强化学习策略闭环版】：动态注入 Q 值，引导大模型进行策略升级
        """
        # 确保 q_table 被安全初始化，避免报错
        if q_table is None:
            q_table = {}
        
        # 🌟 动态生成策略提示：将 Q 值转化为大模型能听懂的自然语言约束
        policy_feedback = ""
        if q_table:
            policy_feedback = "\n【⚠️ 强化学习系统反馈 (工具近期可靠性评估)】：\n"
            for tool, q_val in q_table.items():
                if q_val < 0:
                    policy_feedback += f"- {tool} 动作: 当前 Q 值为 {q_val}。(极度危险：近期执行严重失败！请尽量避免使用，或必须极度严谨地检查其参数！)\n"
                elif q_val > 0:
                    policy_feedback += f"- {tool} 动作: 当前 Q 值为 {q_val}。(表现优秀，鼓励优先使用)\n"

        """
        【黄金十条唤醒版】专门为动态 DAG 引擎提供纯文本到 JSON 的意图解析能力。
        """
      # 【MapAgent 终极逻辑对齐版 Prompt】
        # 集成了：CoT思维链、物理法则约束、实体原名保护、参数纠错、以及强化学习反馈
      # 【MapAgent 极简奥卡姆剃刀版 Prompt】
# 【MapAgent 终极认知对齐版 Prompt】
        system_instruction = (
            "你是一个高级地图导航规划代理。你负责将用户的自然语言指令拆解为逻辑严密的 DAG 任务流。\n\n"
            
            "【🧱 物理世界法则：工具箱约束】\n"
            "1. search_poi: 负责定位。参数: 'location'(坐标或'current_gps'), 'poi_type'(目标原名), 'sort'('distance'|'rating')。\n"
            "2. plan_route: 负责算路。参数: 'start', 'end', 'waypoints'(数组)。\n"
            "   - 🌟 可选参数 'strategy': 'speed'(默认,红绿灯少/速度快), 'distance'(距离短), 'avoid_congestion'(避堵), 'cost'(免费)。\n"
            "   ⚠️ 致命红线：plan_route 的 'end' 和 'start' 绝对不能填入中文地名！必须是前序坐标引用（如 '$T1.result'）！\n"
            "3. get_weather: 负责查询天气。参数必须叫 'location'！严禁使用 'city'！必须填入目标地点的原名或坐标引用。\n\n"
            "【🚨 严苛防呆约束清单】\n"
            "1. 实体原名保护：用户说'深圳湾公园'，poi_type 就写'深圳湾公园'，绝对禁止脑补和简化。\n"
            "2. 🚫 逻辑红线 (No If/Else)：DAG 引擎是静态单向图，不支持 if/else 逻辑判断！绝对不允许输出 'if', 'judge_condition', 'if_not_rain' 等自己发明的动作！如果用户的指令中带有条件（例如“如果不下雨就导航”），请【直接假设条件成立】，严格按照“范式 B”生成线性任务链。\n"
            "3. 变量引用规范：引用前序结果必须使用带 $ 的格式（如 '$T1.result'）。严禁漏掉 $ 符号。\n"
            "4. 排序意图收敛：'最近'、'评分最高'必须放入 sort 参数，绝对不要拼入 poi_type 中。\n"
            "5. 起终点防重合：plan_route 的起点和终点严禁指向同一个变量。\n"
            "6. 🏥 医疗搜索降级常识：地图API只认'医院'作为基础标签。当用户找'三甲医院'时，poi_type 必须强制简化为'医院'或'综合医院'，绝不能带上'三甲'二字，避免字面匹配失败导致舍近求远！\n\n"
            "👉只有当用户明确提问天气或带有天气条件时，才进行天气规划\n\n"
            "【🎯 输出模板示例 】\n"
            "用户：'如果不下雨，就带我去光明蓝鲸世界，找红绿灯最少的路'\n"
            "{\n"
            "  \"thought\": \"用户有天气假设条件，但我严禁使用 if 动作，必须直接假设条件成立。我将使用范式 B：T1搜索拿到坐标，T2使用坐标查询天气，T3使用坐标规划速度优先路线（代表红绿灯少）。\",\n"
            "  \"tasks\": [\n"
            "    {\"task_id\": \"T1\", \"action\": \"search_poi\", \"params\": {\"location\": \"current_gps\", \"poi_type\": \"光明蓝鲸世界\", \"sort\": \"distance\"}, \"dependencies\": []},\n"
            "    {\"task_id\": \"T2\", \"action\": \"get_weather\", \"params\": {\"location\": \"$T1.result\"}, \"dependencies\": [\"T1\"]},\n"
            "    {\"task_id\": \"T3\", \"action\": \"plan_route\", \"params\": {\"start\": \"current_gps\", \"end\": \"$T1.result\", \"strategy\": \"speed\"}, \"dependencies\": [\"T1\", \"T2\"]}\n"
            "  ]\n"
            "}\n"
            + policy_feedback
        )
        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_query}
        ]
        
        # ... 后面保持你原有的调用代码不变 ...

        if self._can_call_live():
            try:
                print("[llm] 正在向本地模型发送推理请求...")
                response_text = self._chat(messages, temperature=0.1)
                return response_text
            except Exception as e:
                print(f"[warn] 调用本地大模型出现异常: {e}")
                raise e
        else:
            raise RuntimeError("大模型服务未配置。")
    def generate_friendly_response(self, user_query: str, route_report: dict) -> str:
        return self.generate_friendly_response_with_meta(user_query, route_report)["text"]

    def generate_friendly_response_with_meta(self, user_query: str, route_report: dict) -> dict:
        """
        【NLG 语音生成模块】将冰冷的 JSON 路线数据转化为带温度的自然语言播报。
        """
        if not route_report or "distance_km" not in route_report:
            return {"text": "抱歉，我未能成功为您规划出路线，请稍后再试。", "source": "fallback"}

        # 为了防止路线步骤太多（比如一百多步）撑爆大模型上下文，我们只截取前 5 步主要指引给它看
        pois = route_report.get("pois") or []
        destination = next(
            (poi for poi in pois if isinstance(poi, dict) and poi.get("role") == "destination"),
            pois[0] if pois and isinstance(pois[0], dict) else {},
        )
        eta_minutes = route_report.get("eta_minutes")
        try:
            eta_minutes = round(float(eta_minutes), 1)
        except (TypeError, ValueError):
            pass

        report_summary = {
            "destination_name": destination.get("name") or route_report.get("destination_name") or route_report.get("destination") or "目的地",
            "destination_address": destination.get("address") or route_report.get("destination_address") or "",
            "destination_rating": destination.get("rating") or route_report.get("destination_rating"),
            "distance_km": route_report.get("distance_km"),
            "eta_minutes": eta_minutes,
            "traffic_lights": route_report.get("traffic_lights", 0),
            "steps": route_report.get("steps", [])[:2],
        }
        road_status = route_report.get("road_status")
        if road_status and road_status != "路况未知":
            report_summary["road_status"] = road_status
        weather = route_report.get("weather")
        if weather and weather != "未知":
            report_summary["weather"] = weather

        system_instruction = (
            "你是一个自然、贴心的车载出行助手。请像真人副驾驶一样说话，不要写成报告，不要使用“已根据”“为您规划到”这种公文腔。\n\n"
            "【输入信息】\n"
            f"1. 用户的需求: {user_query}\n"
            f"2. 导航数据: {json.dumps(report_summary, ensure_ascii=False)}\n\n"
            "【润色要求】\n"
            "1. 开头像身边朋友一样自然，可以轻快、温柔或俏皮一点，但不要每次都用“可以”开头；可以换成“好嘞”“走起”“我看了一下”“这条路挺顺手”等自然表达。\n"
            "2. 只有导航数据里明确提供 weather 时，才可以说明天气；如果没有 weather 字段，回复中绝对不要出现天气、晴、雨、阴、多云、温度、气温等天气相关内容。\n"
            "3. 轻描淡写带出总距离、预计耗时、红绿灯数量，不要堆参数。\n"
            "4. 必须说出目的地的具体名字；如果用户提到评分、口碑、最高评分，并且导航数据里有 destination_rating，要自然回答评分。\n"
            "5. 只有用户原话明确提到少红绿灯、避雨、避堵等偏好时，才回应这个偏好；不要把系统默认策略说成用户偏好。\n"
            "6. 允许适度加入欢乐、安心的氛围，可以分段写，最后一段可以给用户一些出行上的建议或者祝福的话语，类似于智能小贴士。\n"
            "7. 只说前方一两步关键动作，严禁编造未在 steps 中出现的道路名。\n"
            "8. 100到 200 字，口语自然，不要像技术日志。"
        )

        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": "请根据以上数据为我生成语音播报文案。"}
        ]

        try:
            print("\n[narration] 正在将数据转化为拟人化语音回复...")
            # 这里的 temperature 可以稍微调高一点(0.5)，让语言更生动丰富
            return {"text": self._chat(messages, temperature=0.5).strip(), "source": "llm"}
        except Exception as e:
            print(f"[warn] 生成语音回复失败: {e}")
            return {
                "text": self._fallback_friendly_response(user_query, route_report),
                "source": "fallback",
                "error": str(e),
            }

    def _fallback_friendly_response(self, user_query: str, route_report: dict) -> str:
        distance = route_report.get("distance_km", "--")
        eta = route_report.get("eta_minutes", "--")
        lights = route_report.get("traffic_lights", 0)
        road_status = route_report.get("road_status") or "路况未知"
        weather = route_report.get("weather")

        pois = route_report.get("pois") or []
        destination_name = route_report.get("destination_name") or "目的地"
        destination_rating = route_report.get("destination_rating")
        if pois and isinstance(pois[0], dict):
            destination_name = pois[0].get("name") or destination_name
            destination_rating = pois[0].get("rating") or destination_rating

        steps = [str(step) for step in route_report.get("steps", []) if str(step).strip()]
        first_step = steps[0] if steps else "从当前位置出发"
        second_step = steps[1] if len(steps) > 1 else ""

        weather_text = f"那边现在{weather}，" if weather and weather != "未知" else ""
        follow_step = f"随后{second_step}。" if second_step else ""
        openings = ["好嘞", "走起", "我看了一下", "安排上啦"]
        opening = openings[sum(ord(char) for char in user_query) % len(openings)]
        rating_text = f"，评分{destination_rating:.1f}" if isinstance(destination_rating, (int, float)) else ""
        return (
            f"{opening}，{weather_text}去{destination_name}{rating_text}这条路我帮你看好了。"
            f"全程大约{distance}公里，开过去约{eta}分钟，路上大概{lights}个红绿灯，{road_status}。"
            f"先{first_step}。{follow_step}慢慢来，路线已经在地图上标好啦。"
        )
    def generate_narration(
        self,
        request: RoutePlanRequest,
        environment: EnvironmentSnapshot,
        execution_plan: ExecutionPlan,
        validation: ValidationReport,
    ) -> str:
        prompt = self.narrator_template.format(
            request_context=request.merged_context(),
            environment_context=self._dump_json(environment.model_dump()),
            execution_context=self._dump_json(execution_plan.model_dump()),
            validation_context=self._dump_json(validation.model_dump()),
        )
        if self._can_call_live():
            try:
                messages = [{"role": "user", "content": prompt}]
                return self._chat(messages, temperature=0.4).strip()
            except Exception:
                pass
        return self._fallback_narration(request, environment, execution_plan, validation)

    def _can_call_live(self) -> bool:
        return bool(getattr(self, "base_url", ""))

    # 【修改点 2】修改底层的请求方法，使其支持 messages 数组
    def _chat(self, messages: list[dict], temperature: float = 0.2) -> str:
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key and self.api_key != "EMPTY":
            headers["Authorization"] = f"Bearer {self.api_key}"

        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json={
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
            },
            timeout=60, # 给予本地大模型充分的思考时间
        )
        response.raise_for_status()
        payload = response.json()
        return payload["choices"][0]["message"]["content"]

    def _parse_dag_from_text(self, text: str) -> PlanningDAG | None:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        payload = json.loads(text[start : end + 1])

        nodes = []
        edges = []

        
        if "tasks" in payload:
            # 建立 action 与系统 NodeType 的映射关系
            action_to_type_map = {
                "retrieve_user_profile": "information_gathering",
                "search_poi": "resource_search",
                "check_traffic": "information_gathering",
                "plan_route": "route_search",
                "get_weather_info": "information_gathering"
            }
            
            for i, task in enumerate(payload["tasks"]):
                # 【修改点：增强容错】如果大模型没有吐出 task_id，我们就按顺序自动给它编一个（如 T1, T2）
                task_id = task.get("task_id") or task.get("id") or f"T{i+1}"
                task_id = str(task_id).strip()
                if not task_id:
                    task_id = f"T{i+1}"
                
                # 如果大模型没有吐出 action，给个默认动作名称
                action = task.get("action") or task.get("name") or "未命名动作"
                action = str(action).strip()
                if not action:
                    action = "未命名动作"

                # 将微调的 action 映射到系统的 type，不知道的默认设为 route_search
                node_type = action_to_type_map.get(action, "route_search")
                
                nodes.append(DagNode(
                    id=task_id,
                    name=action,
                    type=node_type,
                    # 将微调模型输出的具体参数保存在推理过程中，方便系统查阅
                    reasoning=json.dumps(task.get("params", {}), ensure_ascii=False),
                    metadata=task.get("params", {})
                ))

                # 将微调数据里的 dependencies 转换为系统的 DagEdge 有向边
                for dep in task.get("dependencies", []):
                    edges.append(DagEdge(source=dep, target=task_id))
            
            # 为了保证系统底层的验证器不报错，强制加一个 output 节点作为收尾
            out_id = "output_node"
            nodes.append(DagNode(id=out_id, name="输出规划结果", type="output", reasoning="整合所有任务输出"))
            sources_in_edges = {edge.source for edge in edges}
            for n in nodes:
                if n.id != out_id and n.id not in sources_in_edges:
                    edges.append(DagEdge(source=n.id, target=out_id))

        # 兼容系统原有的标准数据格式
        elif "nodes" in payload:
            nodes = [
                DagNode(
                    id=node["id"],
                    name=node["name"],
                    type=node["type"],
                    reasoning=node.get("reasoning"),
                    metadata=node.get("metadata", {}),
                )
                for node in payload.get("nodes", [])
            ]
            for edge in payload.get("edges", []):
                if isinstance(edge, dict):
                    edges.append(DagEdge(source=edge["source"], target=edge["target"], label=edge.get("label")))
                elif isinstance(edge, list) and len(edge) >= 2:
                    edges.append(DagEdge(source=edge[0], target=edge[1]))

        if not nodes:
            return None

        return PlanningDAG(nodes=nodes, edges=edges, summary=payload.get("summary", "模型推理成功"))

    def _fallback_dag(
        self,
        request: RoutePlanRequest,
        environment: EnvironmentSnapshot,
        reflection_context: str | None,
    ) -> PlanningDAG:
        # ... (保持原样即可，如果大模型报错会进入这里) ...
        nodes: list[DagNode] = [
            DagNode(
                id="n1",
                name="采集天气、路况与交通管制信息",
                type="information_gathering",
                reasoning="先确认真实环境，避免规划建立在错误前提上。",
            ),
            DagNode(
                id="n2",
                name="提取用户时效、车辆与风险约束",
                type="constraint_extraction",
                reasoning="把自然语言需求收束成可验证的硬约束和软约束。",
            ),
        ]
        edges: list[DagEdge] = []
        node_index = 3
        prerequisite_ids = ["n1", "n2"]

        if request.vehicle_type == "ev" or environment.factbook.get("remaining_range_km") is not None:
            resource_id = f"n{node_index}"
            nodes.append(
                DagNode(
                    id=resource_id,
                    name="搜索补能节点与续航可达半径",
                    type="resource_search",
                    reasoning="纯电或低续航场景必须先确认补能闭环。",
                )
            )
            edges.append(DagEdge(source="n2", target=resource_id))
            prerequisite_ids.append(resource_id)
            node_index += 1

        route_search_id = f"n{node_index}"
        nodes.append(
            DagNode(
                id=route_search_id,
                name="生成候选通行走廊与备选路线",
                type="route_search",
                reasoning="在约束内枚举可行候选，再做筛选。",
            )
        )
        for prerequisite_id in prerequisite_ids:
            edges.append(DagEdge(source=prerequisite_id, target=route_search_id))
        node_index += 1

        if (
            request.vehicle_type in {"truck", "hazmat_truck", "ambulance", "rescue_vehicle"}
            or environment.restricted_zones
            or environment.warnings
        ):
            safety_id = f"n{node_index}"
            nodes.append(
                DagNode(
                    id=safety_id,
                    name="执行限重、限高、敏感区域与安全规则校验",
                    type="safety_check",
                    reasoning="特殊车辆和高风险场景需要显式安全校验。",
                )
            )
            edges.append(DagEdge(source=route_search_id, target=safety_id))
            decision_source_id = safety_id
            node_index += 1
        else:
            decision_source_id = route_search_id

        decision_id = f"n{node_index}"
        nodes.append(
            DagNode(
                id=decision_id,
                name="综合时效、风险和资源约束确定执行策略",
                type="decision",
                reasoning="将候选路线按优先级聚合成单一执行方案。",
            )
        )
        edges.append(DagEdge(source=decision_source_id, target=decision_id))
        node_index += 1

        output_reasoning = "输出最终任务 DAG 供决策代理执行。"
        if reflection_context:
            nodes.append(
                DagNode(
                    id=f"n{node_index}",
                    name="根据上一轮校验失败原因修正规划",
                    type="reflection",
                    reasoning="上一轮出现校验错误，需要在计划层先消掉明显冲突。",
                )
            )
            edges.append(DagEdge(source=decision_id, target=f"n{node_index}"))
            decision_id = f"n{node_index}"
            node_index += 1
            output_reasoning = "输出已经结合错误上下文修订后的 DAG。"

        nodes.append(
            DagNode(
                id=f"n{node_index}",
                name="输出可执行任务与说明",
                type="output",
                reasoning=output_reasoning,
            )
        )
        edges.append(DagEdge(source=decision_id, target=f"n{node_index}"))

        summary = "优先建立环境与约束事实，再搜索候选路线，并在必要时加入补能/合规/反思节点。"
        return PlanningDAG(nodes=nodes, edges=edges, summary=summary)

    def _fallback_narration(
        self,
        request: RoutePlanRequest,
        environment: EnvironmentSnapshot,
        execution_plan: ExecutionPlan,
        validation: ValidationReport,
    ) -> str:
        first_step = execution_plan.route_steps[0].instruction if execution_plan.route_steps else "先确认主通道可用。"
        warning_text = "；".join(issue.message for issue in validation.issues if issue.level == "warning")
        result = (
            f"建议从{request.start}前往{request.destination}时采用“{execution_plan.strategy}”策略，"
            f"当前估算里程约{execution_plan.estimated_distance_km}公里，耗时约{execution_plan.estimated_minutes}分钟。"
            f"当前环境为{environment.weather}，系统已按 {environment.speed_cap_kmh:.0f} km/h 的安全上限做约束。"
            f"执行上先{first_step}"
        )
        if warning_text:
            result += f" 另外需要注意：{warning_text}。"
        elif environment.warnings:
            result += f" 另外需要注意：{'；'.join(environment.warnings[:2])}。"
        return result

    def _dump_json(self, payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2)
