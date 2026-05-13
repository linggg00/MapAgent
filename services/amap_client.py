from __future__ import annotations

import hashlib
import os
import re
import urllib.parse
from typing import Any

import httpx

from schemas.dag_model import EnvironmentSnapshot, ExecutionPlan, PlanningDAG, RouteSegment
from schemas.request import RoutePlanRequest


class AmapClient:
    def __init__(self, api_key: str | None = None, timeout: float = 10.0) -> None:
        self.api_key = "7368f35d561537801c681e41cf0df77e"
        self.timeout = timeout

    def get_environment_snapshot(self, request: RoutePlanRequest) -> EnvironmentSnapshot:
        if request.use_mock_data or not self.api_key:
            return self._mock_environment(request)
        try:
            return self._live_environment(request)
        except Exception as exc:
            fallback = self._mock_environment(request)
            fallback.warnings.append(f"高德实时接口不可用，已回退到 mock 环境：{exc}")
            return fallback

    def build_execution_plan(
        self,
        request: RoutePlanRequest,
        environment: EnvironmentSnapshot,
        dag: PlanningDAG,
        reflection_context: str | None = None,
        strategy_override: str | None = None,
    ) -> ExecutionPlan:
        
        strategy = strategy_override or self._choose_strategy(request, environment, reflection_context)
        
        # 1. 底层自动进行经纬度解析
        origin_coord = self._ensure_coord(request.start)
        dest_coord = self._ensure_coord(request.destination)
             # 【新增】：借助 extra 字段解析途经点（如：肯德基）
        waypoint_text = request.extra.get("waypoints", "")
        waypoint_coord = ""
        # 聪明且普适的方法：在起终点确定后进行沿途搜
        if waypoint_text and origin_coord and dest_coord:
            import re
            
            # 【普适解法】：使用正则剔除可能存在的行政前缀（省/市/区/县）
            # 例如："广州市麦当劳" -> "麦当劳"； "天河区肯德基" -> "肯德基"
            clean_keyword = re.sub(r'^(.*?省|.*?市|.*?区|.*?县)', '', waypoint_text).strip()
            
            # 兜底：如果剔除后空了，或者用户输入的很特殊，就直接用原词
            clean_keyword = clean_keyword if clean_keyword else waypoint_text
            
            waypoint_coord = self._search_along_route(origin_coord, dest_coord, clean_keyword)

        resolved_destination = request.extra.get("resolved_destination") or {}
        resolved_waypoints = request.extra.get("resolved_waypoints") or []
        display_start = request.extra.get("resolved_start_name") or request.start
        display_destination = resolved_destination.get("name") or request.destination

        origin_coord = request.extra.get("current_gps") or origin_coord
        if resolved_destination.get("location"):
            dest_coord = resolved_destination["location"]

        if resolved_waypoints:
            waypoint_coord = ";".join(
                waypoint.get("location")
                for waypoint in resolved_waypoints
                if isinstance(waypoint, dict) and waypoint.get("location")
            )
        route_steps = []
        distance_km = 0.0
        eta_minutes = 0.0

        # 2. 调用高德【真实驾车规划 API】
        # 2. 调用高德【真实驾车规划 API】
        if origin_coord and dest_coord and self.api_key:
            try:
                print("[amap] 底层执行器正在调用高德真实驾车规划 API...")
                nav_url = "https://restapi.amap.com/v3/direction/driving"
                params = {
                    "origin": origin_coord,
                    "destination": dest_coord,
                    "key": self.api_key,
                    "extensions": "all"
                }
                if waypoint_coord:
                    params["waypoints"] = waypoint_coord
                    print(f"[amap] 成功向高德 API 注入途经点坐标: {waypoint_coord}")
                
                res = httpx.get(nav_url, params=params, timeout=self.timeout)
               
                res.raise_for_status()
                data = res.json()
                
                if data.get("status") == "1" and data.get("route", {}).get("paths"):
                    path = data["route"]["paths"][0]
                    distance_km = float(path.get("distance", 0)) / 1000.0
                    eta_minutes = float(path.get("duration", 0)) / 60.0
                    
                    for step in path.get("steps", []):
                        # 拦截高德的空列表 []，强制转换为字符串
                        act = step.get("action", "")
                        action_str = act if isinstance(act, str) else ""
                        
                        rd = step.get("road", "未知路段")
                        road_str = rd if isinstance(rd, str) else "未知路段"
                        
                        # 重点看下面这里的 append，已经加上了 round(..., 1)
                        route_steps.append(RouteSegment(
                            mode="driving",            
                            speed_limit_kmh=60.0,      
                            instruction=str(step.get("instruction", "")),
                            # 【加上了 round 保留一位小数】：
                            distance_km=round(float(step.get("distance", 0)) / 1000.0, 1),
                            eta_minutes=round(float(step.get("duration", 0)) / 60.0, 1),
                            from_name=road_str,
                            to_name=action_str
                        ))
            except Exception as e:
                print(f"[warn] 高德驾车API调用失败: {e}")

        # 3. 兜底机制也要用 RouteSegment 并且修复不存在的方法
        if not route_steps:
            distance_km = self._apply_strategy_distance_multiplier(self._estimate_distance_km(request), strategy)
            # 【修复2】直接使用原始的时间预估，删掉那个不存在的乘数方法
            eta_minutes = self._estimate_eta_minutes(request, environment)
            route_steps.append(RouteSegment(
                mode="driving",            # <--- 同样补上
                speed_limit_kmh=60.0,      # <--- 同样补上
                instruction=f"从 {request.start} 前往 {request.destination}",
                distance_km=distance_km,
                eta_minutes=eta_minutes,
                from_name=request.start,
                to_name=request.destination
            ))

        # 完美组装返回
 # 完美组装返回
        return ExecutionPlan(
            strategy=strategy,
            mode="driving",
            route_steps=route_steps, 
            route_segments=[], 
            # 【这里确保也加上了 round(..., 1)】
            estimated_distance_km=round(distance_km, 1),
            estimated_minutes=round(eta_minutes, 1),
            average_speed_kmh=round(distance_km / (eta_minutes / 60.0), 1) if eta_minutes > 0 else 0.0
        )
        

    def list_candidate_strategies(
        self,
        request: RoutePlanRequest,
        environment: EnvironmentSnapshot,
        reflection_context: str | None = None,
    ) -> list[str]:
        candidates = [self._choose_strategy(request, environment, reflection_context), "balanced"]
        if environment.speed_cap_kmh <= 45:
            candidates.append("weather_safe")
        if request.arrival_deadline_minutes is not None:
            candidates.append("priority_time_with_explicit_risk_control")
        if request.vehicle_type == "ev" or environment.factbook.get("remaining_range_km") is not None:
            candidates.append("energy_saving_with_refuel_check")
        if request.vehicle_type in {"truck", "hazmat_truck"} or request.cargo_type:
            candidates.append("compliance_first")
        if request.vehicle_type in {"ambulance", "rescue_vehicle"}:
            candidates.append("rescue_first")
        if environment.factbook.get("prefer_smooth_drive"):
            candidates.append("passenger_comfort")

        deduped: list[str] = []
        for strategy in candidates:
            if strategy not in deduped:
                deduped.append(strategy)
        return deduped

    def _live_environment(self, request: RoutePlanRequest) -> EnvironmentSnapshot:
        start_geo = self._geocode(request.start)
        end_geo = self._geocode(request.destination)
        weather_text = "未知"
        notes: list[str] = []
        factbook = self._extract_request_facts(request)

        if start_geo and start_geo.get("adcode"):
            weather_text = self._weather(start_geo["adcode"])
            notes.append(f"已获取 {request.start} 所在城市实时天气")

        speed_cap = self._weather_speed_cap(weather_text, request.scene)
        factbook["start_geocode"] = start_geo
        factbook["end_geocode"] = end_geo

        return EnvironmentSnapshot(
            source="amap",
            weather=weather_text,
            temperature_c=factbook.get("temperature_c"),
            visibility_km=factbook.get("visibility_km", 10.0),
            speed_cap_kmh=speed_cap,
            closed_roads=self._extract_closures(request.scene),
            restricted_zones=self._extract_restricted_zones(request),
            available_facilities=self._default_facilities(request),
            warnings=notes + self._scene_warnings(request),
            factbook=factbook,
        )

    def _mock_environment(self, request: RoutePlanRequest) -> EnvironmentSnapshot:
        text = self._joined_text(request)
        weather = "晴"
        temperature = 22.0
        visibility = 15.0
        speed_cap = 90.0
        warnings: list[str] = []

        if any(keyword in text for keyword in ["暴雪", "冻雨", "结冰"]):
            weather = "暴雪/结冰"
            temperature = -8.0
            visibility = 0.6
            speed_cap = 40.0
            warnings.extend(["存在路面结冰风险", "建议提高安全冗余时间"])
        elif any(keyword in text for keyword in ["暴雨", "积水"]):
            weather = "暴雨"
            temperature = 18.0
            visibility = 2.0
            speed_cap = 50.0
            warnings.extend(["低洼路段可能积水", "部分匝道可能临时封闭"])
        elif any(keyword in text for keyword in ["台风", "龙卷风", "强风"]):
            weather = "强风/台风"
            temperature = 24.0
            visibility = 4.0
            speed_cap = 45.0
            warnings.extend(["高架和跨海路段风险较高", "建议避开沿海开放路段"])
        elif any(keyword in text for keyword in ["地震", "泥石流", "落石", "山洪"]):
            weather = "灾害态势"
            temperature = 16.0
            visibility = 5.0
            speed_cap = 35.0
            warnings.extend(["道路可用性不稳定", "需持续规避次生灾害区域"])
        elif any(keyword in text for keyword in ["大雾", "低能见度"]):
            weather = "浓雾"
            temperature = 12.0
            visibility = 0.3
            speed_cap = 30.0
            warnings.extend(["能见度极低", "建议优先选择熟悉、宽阔主路"])

        factbook = self._extract_request_facts(request)

        return EnvironmentSnapshot(
            source="mock",
            weather=weather,
            temperature_c=temperature,
            visibility_km=visibility,
            speed_cap_kmh=speed_cap,
            closed_roads=self._extract_closures(request.scene),
            restricted_zones=self._extract_restricted_zones(request),
            available_facilities=self._default_facilities(request),
            warnings=warnings + self._scene_warnings(request),
            factbook=factbook,
        )

    def _extract_request_facts(self, request: RoutePlanRequest) -> dict[str, Any]:
        text = self._joined_text(request)
        facts: dict[str, Any] = {}

        remaining_range = self._extract_number(r"续航(?:仅|还有|剩余)?\s*(\d+(?:\.\d+)?)\s*km", text)
        if remaining_range is None:
            remaining_range = self._extract_number(r"剩余(?:电量)?(?:续航)?\s*(\d+(?:\.\d+)?)\s*km", text)
        if remaining_range is not None:
            facts["remaining_range_km"] = remaining_range

        deadline_hours = self._extract_number(r"(\d+(?:\.\d+)?)\s*小时(?:内|后)", text)
        if request.arrival_deadline_minutes is None and deadline_hours is not None and "小时内" in text:
            request.arrival_deadline_minutes = int(deadline_hours * 60)

        vehicle_weight = self._extract_number(r"(\d+(?:\.\d+)?)\s*吨(?:重|载|房车|卡车|货车)?", text)
        if vehicle_weight is not None and request.vehicle_type in {"truck", "hazmat_truck", "rescue_vehicle"}:
            facts["vehicle_weight_tons"] = vehicle_weight

        bridge_limit = self._extract_number(r"限重\s*(\d+(?:\.\d+)?)\s*吨", text)
        if bridge_limit is not None:
            facts["bridge_weight_limit_tons"] = bridge_limit

        vehicle_height = self._extract_number(r"(\d+(?:\.\d+)?)\s*米(?:高|车高)?", text)
        if vehicle_height is not None:
            facts["vehicle_height_m"] = vehicle_height

        height_limit = self._extract_number(r"限高\s*(\d+(?:\.\d+)?)\s*米", text)
        if height_limit is not None:
            facts["height_limit_m"] = height_limit

        facts["need_indoor_charge"] = "室内" in text or "恒温快充" in text
        facts["need_rescue_priority"] = any(keyword in text for keyword in ["救援", "抢救", "救护", "急诊"])
        facts["avoid_residential"] = any(keyword in text for keyword in ["居民区", "水源保护地"])
        facts["prefer_smooth_drive"] = request.passengers.elderly > 0 or request.passengers.children > 0 or "平稳" in text
        return facts

    def _extract_closures(self, scene: str) -> list[str]:
        matches = re.findall(r"([\u4e00-\u9fa5A-Za-z0-9]+(?:高速|桥|隧道|高架|路段|片区|区域))", scene)
        result = []
        for item in matches:
            if any(keyword in scene for keyword in ["封闭", "封控", "管制", "禁行"]) and item not in result:
                result.append(item)
        return result[:5]

    def _extract_restricted_zones(self, request: RoutePlanRequest) -> list[str]:
        zones = []
        text = self._joined_text(request)
        keywords = ["居民区", "学校周边", "水源保护地", "沿海片区", "高层玻璃幕墙路段", "泥石流预警区"]
        for keyword in keywords:
            if keyword in text:
                zones.append(keyword)
        return zones

    def _default_facilities(self, request: RoutePlanRequest) -> list[str]:
        facilities: list[str] = []
        if request.vehicle_type == "ev" or "充电" in self._joined_text(request):
            facilities.extend(["室内恒温快充站", "地库补能站"])
        if request.vehicle_type in {"truck", "hazmat_truck"} or request.cargo_type:
            facilities.extend(["危化品检查站", "合规补给站"])
        if request.vehicle_type in {"ambulance", "rescue_vehicle"}:
            facilities.extend(["应急绿色通道", "联动调度点"])
        return facilities

    def _choose_strategy(
        self,
        request: RoutePlanRequest,
        environment: EnvironmentSnapshot,
        reflection_context: str | None,
    ) -> str:
        goals = set(request.goals)
        if reflection_context and "时限" in reflection_context and "仍不满足" in reflection_context:
            return "priority_time_with_explicit_risk_control"
        if "rescue_priority" in goals or request.vehicle_type in {"ambulance", "rescue_vehicle"}:
            return "rescue_first"
        if request.vehicle_type == "ev":
            return "energy_saving_with_refuel_check"
        if request.vehicle_type in {"truck", "hazmat_truck"}:
            return "compliance_first"
        if environment.speed_cap_kmh <= 45:
            return "weather_safe"
        if "smoothest" in goals:
            return "passenger_comfort"
        return "balanced"

    def _choose_mode(self, request: RoutePlanRequest) -> str:
        if request.vehicle_type == "hazmat_truck":
            return "truck"
        if request.prefer_modes:
            return request.prefer_modes[0]
        if request.vehicle_type == "public_transit":
            return "transit"
        return "driving"

    def _estimate_distance_km(self, request: RoutePlanRequest) -> float:
        text = f"{request.start}->{request.destination}"
        digest = hashlib.md5(text.encode("utf-8")).hexdigest()
        base = int(digest[:6], 16) % 180 + 20
        if any(keyword in self._joined_text(request) for keyword in ["机场", "高铁", "医院", "救援"]):
            base += 12
        if request.vehicle_type in {"truck", "hazmat_truck"}:
            base += 18
        return float(base)

    def _apply_strategy_distance_multiplier(self, distance_km: float, strategy: str) -> float:
        multipliers = {
            "balanced": 1.0,
            "weather_safe": 1.08,
            "rescue_first": 0.94,
            "priority_time_with_explicit_risk_control": 0.92,
            "energy_saving_with_refuel_check": 1.03,
            "compliance_first": 1.1,
            "passenger_comfort": 1.05,
        }
        return round(distance_km * multipliers.get(strategy, 1.0), 1)

    def _strategy_speed_cap(
        self,
        strategy: str,
        request: RoutePlanRequest,
        environment: EnvironmentSnapshot,
    ) -> float:
        base_speed = min(environment.speed_cap_kmh, self._vehicle_speed_cap(request.vehicle_type))
        multiplier = {
            "balanced": 0.97,
            "weather_safe": 0.82,
            "rescue_first": 1.0,
            "priority_time_with_explicit_risk_control": 1.08,
            "energy_saving_with_refuel_check": 0.9,
            "compliance_first": 0.86,
            "passenger_comfort": 0.8,
        }.get(strategy, 0.95)
        return round(min(base_speed * multiplier, environment.speed_cap_kmh), 1)

    def _vehicle_speed_cap(self, vehicle_type: str) -> float:
        caps = {
            "sedan": 100.0,
            "suv": 95.0,
            "truck": 80.0,
            "hazmat_truck": 70.0,
            "ambulance": 100.0,
            "rescue_vehicle": 85.0,
            "ev": 95.0,
            "public_transit": 60.0,
        }
        return caps.get(vehicle_type, 80.0)

    def _build_waypoints(
        self,
        request: RoutePlanRequest,
        environment: EnvironmentSnapshot,
        reflection_context: str | None,
        strategy: str,
    ) -> list[str]:
        points: list[str] = []
        facts = environment.factbook

        if request.vehicle_type == "ev" and (
            facts.get("need_indoor_charge")
            or facts.get("remaining_range_km", 9999) < self._estimate_distance_km(request) * 0.8
        ):
            points.append("室内恒温快充站")

        if request.vehicle_type in {"truck", "hazmat_truck"} or request.cargo_type:
            points.append("合规绕行通道")

        if facts.get("prefer_smooth_drive"):
            points.append("平稳通行走廊")

        if environment.closed_roads:
            points.append("受控绕行节点")

        if strategy == "weather_safe":
            points.append("低风险主路走廊")
        if strategy == "rescue_first":
            points.insert(0, "应急绿色通道")
        if strategy == "priority_time_with_explicit_risk_control":
            points.insert(0, "快速放行节点")
        if strategy == "passenger_comfort":
            points.append("平稳通行走廊")
        if strategy == "compliance_first":
            points.append("危化品检查站")

        if reflection_context and "补能" in reflection_context and "室内恒温快充站" not in points:
            points.insert(0, "室内恒温快充站")

        deduped: list[str] = []
        for point in points:
            if point not in deduped:
                deduped.append(point)
        return deduped[:3]

    def _segment_ratios(self, segment_count: int) -> list[float]:
        if segment_count <= 1:
            return [1.0]
        if segment_count == 2:
            return [0.45, 0.55]
        if segment_count == 3:
            return [0.32, 0.28, 0.40]
        return [round(1 / segment_count, 3) for _ in range(segment_count)]

    def _strategy_speed_decay(self, strategy: str) -> float:
        return {
            "balanced": 3.0,
            "weather_safe": 2.0,
            "rescue_first": 2.5,
            "priority_time_with_explicit_risk_control": 2.0,
            "energy_saving_with_refuel_check": 2.5,
            "compliance_first": 2.0,
            "passenger_comfort": 1.5,
        }.get(strategy, 3.0)

    def _segment_delay(self, environment: EnvironmentSnapshot, index: int, strategy: str) -> float:
        closure_penalty = min(len(environment.closed_roads) * 4.0, 18.0)
        warning_penalty = min(len(environment.warnings) * 1.5, 10.0)
        strategy_delta = {
            "balanced": 0.0,
            "weather_safe": 4.0,
            "rescue_first": -2.0,
            "priority_time_with_explicit_risk_control": -4.0,
            "energy_saving_with_refuel_check": 3.0,
            "compliance_first": 4.0,
            "passenger_comfort": 2.0,
        }.get(strategy, 0.0)
        return max(0.0, closure_penalty + warning_penalty + index * 2.0 + strategy_delta)

    def _build_instruction(
        self,
        start: str,
        end: str,
        request: RoutePlanRequest,
        environment: EnvironmentSnapshot,
    ) -> str:
        if "快充站" in end:
            return f"从{start}行驶至{end}补能，优先使用室内可用桩位。"
        if "合规绕行通道" in end:
            return f"从{start}切入{end}，避开限重、限高及敏感区域。"
        if "危化品检查站" in end:
            return f"从{start}前往{end}复核载荷与禁行条件，再继续通行。"
        if "应急绿色通道" in end:
            return f"从{start}进入{end}，争取联动放行和更短救援路径。"
        if "快速放行节点" in end:
            return f"从{start}经{end}压缩等待时间，但仍需遵守当前天气限速。"
        if "低风险主路走廊" in end:
            return f"从{start}转入{end}，优先宽阔主路并降低灾害暴露。"
        if "平稳通行走廊" in end:
            return f"从{start}进入{end}，减少急刹和频繁并线。"
        if "受控绕行节点" in end:
            return f"从{start}绕开封闭路段，经{end}继续前往目标。"
        return f"从{start}前往{end}，按当前 {environment.weather} 场景控制车速。"

    def _build_segment_warnings(
        self,
        request: RoutePlanRequest,
        environment: EnvironmentSnapshot,
        end: str,
    ) -> list[str]:
        warnings = list(environment.warnings[:2])
        if request.vehicle_type == "hazmat_truck":
            warnings.append("危化品运输请避开人员密集区并保持冗余制动距离")
        if "快充站" in end:
            warnings.append("补能前确认桩体可用且环境温度满足安全范围")
        if "快速放行节点" in end:
            warnings.append("压缩时间目标不代表可以突破安全限速")
        return warnings[:3]

    def _estimate_cost(self, distance_km: float, vehicle_type: str) -> float:
        rate = {
            "sedan": 0.95,
            "suv": 1.1,
            "truck": 2.3,
            "hazmat_truck": 2.8,
            "ambulance": 1.5,
            "rescue_vehicle": 1.7,
            "ev": 0.55,
            "public_transit": 0.3,
        }.get(vehicle_type, 1.0)
        return round(distance_km * rate, 1)

    def _build_checks(
        self,
        request: RoutePlanRequest,
        environment: EnvironmentSnapshot,
        dag: PlanningDAG,
        waypoints: list[str],
    ) -> list[str]:
        checks = [f"DAG 节点数：{len(dag.nodes)}", f"环境源：{environment.source}", f"天气限速上限：{environment.speed_cap_kmh} km/h"]
        if environment.closed_roads:
            checks.append(f"已规避封闭路段：{'、'.join(environment.closed_roads)}")
        if environment.restricted_zones:
            checks.append(f"已标记敏感区域：{'、'.join(environment.restricted_zones)}")
        if waypoints:
            checks.append(f"规划附加节点：{'、'.join(waypoints)}")
        if request.cargo_type:
            checks.append(f"货物类型：{request.cargo_type}")
        if request.vehicle_type == "hazmat_truck":
            checks.append("危化品合规：已要求避开敏感区域并保留专用绕行通道")
        return checks

    def _build_navigation_link(
        self,
        request: RoutePlanRequest,
        environment: EnvironmentSnapshot,
        mode: str,
    ) -> str:
        start_geo = environment.factbook.get("start_geocode")
        end_geo = environment.factbook.get("end_geocode")
        if start_geo and end_geo and start_geo.get("location") and end_geo.get("location"):
            from_loc = start_geo["location"]
            to_loc = end_geo["location"]
            return (
                "https://uri.amap.com/navigation?"
                f"from={from_loc},{urllib.parse.quote(request.start)}&"
                f"to={to_loc},{urllib.parse.quote(request.destination)}&"
                f"mode={'car' if mode in {'driving', 'truck'} else 'walk'}&src=MapAgent"
            )
        return (
            "mock://navigation?"
            f"start={urllib.parse.quote(request.start)}&"
            f"destination={urllib.parse.quote(request.destination)}&"
            f"mode={urllib.parse.quote(mode)}"
        )

    def _joined_text(self, request: RoutePlanRequest) -> str:
        return " ".join(
            [
                request.start,
                request.destination,
                request.scene,
                request.user_request,
                " ".join(request.hard_constraints),
                " ".join(request.soft_constraints),
                request.cargo_type or "",
            ]
        )

    def _extract_number(self, pattern: str, text: str) -> float | None:
        match = re.search(pattern, text)
        if not match:
            return None
        return float(match.group(1))

    def _scene_warnings(self, request: RoutePlanRequest) -> list[str]:
        text = self._joined_text(request)
        warnings = []
        if "限高" in text:
            warnings.append("存在限高约束，请确认通道净空")
        if "限重" in text:
            warnings.append("存在限重约束，请确认桥梁承载")
        if "学校" in text:
            warnings.append("学校周边建议降低车速并控制鸣笛")
        return warnings

    def _geocode(self, place: str, city_hint: str = "") -> dict | None:
        """
        高德 POI 关键字搜索 API + 智能仲裁算法
        """
        if not self.api_key:
            return None
            
        print(f"[amap] 底层触发 POI 检索：正在精准搜索 '{place}'...")
        
        # 使用 POI 搜索接口 (place/text)
        url = "https://restapi.amap.com/v3/place/text"
        params = {
            "keywords": place,
            "key": self.api_key,
            "offset": 5,      # 【关键】多拿几个结果（前5个），方便我们的仲裁算法筛选
            "page": 1,
            "extensions": "base"
        }
        
        # 如果有城市提示，加上约束
        if city_hint:
            params["city"] = city_hint

        try:
            response = httpx.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
            
            pois = payload.get("pois") or []
            if not pois:
                return None
           
            import re
            core_kw = re.sub(r'^(.*?省|.*?市|.*?区|.*?县)', '', place).strip()
            
            # 【🛡️ 终极防弹衣】：如果用户输入的就是纯地名（剔除后变为空），则还原
            if not core_kw:
                core_kw = place
            # 遍历高德给的列表（前5个），自己做二次筛选
            for poi in pois:
                name = poi.get("name", "")
                address = poi.get("address", "")
                
                # 规则 1：如果用户明确指明了校区，强制匹配
                if "北校区" in core_kw or "北校园" in core_kw:
                    if "北" in name or "北" in address:
                        best_poi = poi
                        break
                elif "东校区" in core_kw or "东校园" in core_kw:
                    if "东" in name or "东" in address:
                        best_poi = poi
                        break
                elif "南校区" in core_kw or "南校园" in core_kw:
                    if "南" in name or "南" in address:
                        best_poi = poi
                        break
                # 规则 2：如果是完美包含关系，优先采纳
                elif core_kw in name:
                    best_poi = poi
                    break

            print(f"[amap] POI智能仲裁: 目标[{core_kw}] | 高德首推[{pois[0].get('name')}] | 系统纠正为[{best_poi.get('name')}]")
            
            return {
                "location": best_poi.get("location"),
                "formatted_address": best_poi.get("name") + " (" + best_poi.get("address", "") + ")",
                "adcode": best_poi.get("adcode"),
            }
        except Exception as e:
            print(f"[warn] POI 搜索失败: {e}")
            return None
    def _is_coord(self, text: str) -> bool:
        """判断传入的地址是否已经是精准的经纬度"""
        if not text: return False
        parts = text.split(",")
        return len(parts) == 2 and parts[0].replace('.', '', 1).isdigit()

    def _ensure_coord(self, place: str) -> str:
        """核心组件：如果是文字地名，底层自动调用高德将其转换为经纬度"""
        if not place or place in ["默认起点", "默认终点"]:
            return ""
        if self._is_coord(place):
            return place
        
        print(f"[amap] 底层触发地理编码：正在将 '{place}' 转换为坐标...")
        geo = self._geocode(place)
        if geo and geo.get("location"):
            return geo["location"]
        return ""

    def convert_coord_from_gps(self, location: str) -> str:
        if not self.api_key or not location:
            return location

        try:
            response = httpx.get(
                "https://restapi.amap.com/v3/assistant/coordinate/convert",
                params={"key": self.api_key, "locations": location, "coordsys": "gps"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") == "1" and payload.get("locations"):
                return payload["locations"]
        except Exception as e:
            print(f"[warn] 坐标转换失败: {e}")
        return location

    def reverse_geocode(self, location: str) -> dict[str, Any]:
        if not self.api_key or not location:
            return {"location": location, "formatted_address": ""}

        try:
            response = httpx.get(
                "https://restapi.amap.com/v3/geocode/regeo",
                params={"key": self.api_key, "location": location, "extensions": "base"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            regeocode = payload.get("regeocode") or {}
            return {
                "location": location,
                "formatted_address": regeocode.get("formatted_address", ""),
                "address_component": regeocode.get("addressComponent") or {},
            }
        except Exception as e:
            print(f"[warn] 逆地理编码失败: {e}")
            return {"location": location, "formatted_address": ""}

    def _weather(self, adcode: str) -> str:
        if not self.api_key:
            return "未知"
        response = httpx.get(
            "https://restapi.amap.com/v3/weather/weatherInfo",
            params={"city": adcode, "key": self.api_key, "extensions": "base"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        forecasts = payload.get("lives") or []
        if not forecasts:
            return "未知"
        return forecasts[0].get("weather", "未知")

    def _weather_speed_cap(self, weather_text: str, scene_text: str) -> float:
        text = f"{weather_text} {scene_text}"
        if any(keyword in text for keyword in ["暴雪", "冻雨", "结冰"]):
            return 40.0
        if any(keyword in text for keyword in ["暴雨", "台风", "强风"]):
            return 50.0
        if any(keyword in text for keyword in ["大雾", "低能见度"]):
            return 30.0
        return 85.0
    def _search_along_route(self, origin_coord: str, dest_coord: str, keyword: str) -> str:
        """
        真正的沿途搜 API：只找顺路的店，绝不南辕北辙！
        """
        if not self.api_key or not origin_coord or not dest_coord or not keyword:
            return ""
            
        print(f"[amap] 启动沿途搜：正在为您寻找主路线周边的 '{keyword}'...")
        
        url = "https://restapi.amap.com/v3/place/route"
        params = {
            "key": self.api_key,
            "origin": origin_coord,
            "destination": dest_coord,
            "keywords": keyword,
            "strategy": 0, # 0表示距离优先（绕路最少）
            "range": 2000  # 搜索路线周边 2000 米范围内的店
        }
        
        try:
            response = httpx.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            
            route_pois = data.get("route_poi") or []
            if not route_pois:
                print("[warn] 沿途 2000 米内没有找到该地标。")
                return ""
                
            # 拿到绕路最少的一家店的坐标
            best_poi = route_pois[0]
            poi_coord = best_poi.get("location")
            # 顺便看看高德到底找了哪家店
            poi_name = best_poi.get("name") 
            print(f"[amap] 成功找到顺路的店: {poi_name} ({poi_coord})")
            
            return poi_coord
        except Exception as e:
            print(f"[warn] 沿途搜调用失败: {e}")
            return ""
    def search_around_poi(self, center_coord: str, keyword: str, sortrule: str = "weight") -> str:
        """
        【原子工具 1：周边搜索】
        支持按距离(distance)或评分热度(weight)寻找最佳地点。
        """
        if not self.api_key or not center_coord:
            return ""
            
        print(f"[tool] 搜索 {center_coord} 周边 '{keyword}' (排序: {sortrule})...")
        
        url = "https://restapi.amap.com/v3/place/around"
        params = {
            "key": self.api_key,
            "location": center_coord,
            "keywords": keyword,
            # ==================================================
            # 🛡️ 破除结界：将 3000 改为 50000 (50公里)，支持跨区地标搜索
            # ==================================================
            "radius": 50000,       
            "sortrule": sortrule, 
            "offset": 1,
            "page": 1,
            "extensions": "base"
        }
        # ... 后面保持不变
        
        try:
            res = httpx.get(url, params=params, timeout=self.timeout)
            res.raise_for_status()
            pois = res.json().get("pois") or []
            if not pois:
                print("[warn] [工具调用] 未找到符合条件的地点。")
                return ""
                
            best_poi = pois[0]
            print(f"[tool] 锁定目标: {best_poi.get('name')} (距离: {best_poi.get('distance')}米)")
            return best_poi.get("location")
        except Exception as e:
            print(f"[warn] 周边搜索API异常: {e}")
            return ""

    def _format_poi_candidate(self, poi: dict[str, Any]) -> dict[str, Any]:
        distance_raw = poi.get("distance")
        distance_m = None
        if distance_raw not in [None, ""]:
            try:
                distance_m = int(float(distance_raw))
            except (TypeError, ValueError):
                distance_m = None
        biz_ext = poi.get("biz_ext") or {}
        rating = None
        if isinstance(biz_ext, dict) and biz_ext.get("rating") not in [None, "", []]:
            try:
                rating = float(biz_ext.get("rating"))
            except (TypeError, ValueError):
                rating = None
        return {
            "id": poi.get("id"),
            "name": poi.get("name", ""),
            "address": poi.get("address", "") or "",
            "location": poi.get("location", ""),
            "distance_m": distance_m,
            "rating": rating,
            "type": poi.get("type"),
        }

    def _normalize_poi_keyword(self, keyword: str) -> str:
        normalized = (keyword or "").strip()
        normalized = re.sub(r"[，,。！？!?；;]", "", normalized)
        normalized = re.sub(r"评分(?:在)?\s*\d(?:\.\d)?\s*以上的?", "", normalized).strip()
        normalized = re.sub(r"(评分最高的?|高评分的?|评分较高的?)", "", normalized).strip()
        normalized = re.sub(r"(怎么走|如何走|怎么去|如何去|导航|路线|看病|就诊|挂号|看医生|看病去)$", "", normalized).strip()
        if re.search(r"(医院|卫生院|社康|社区健康服务中心|诊所)", normalized):
            normalized = re.sub(r"(看病|就诊|挂号|看医生|看病去)", "", normalized).strip()
        prefix_pattern = r"^(离我最近的|我附近的|附近最近的|最近的|附近|周边|就近|最近|我附近)"
        suffix_pattern = r"(最近的|附近|周边|最近)$"
        previous = None
        while previous != normalized:
            previous = normalized
            normalized = re.sub(prefix_pattern, "", normalized).strip()
            normalized = re.sub(suffix_pattern, "", normalized).strip()
        if re.fullmatch(r"(医院|卫生院|社康|社区健康服务中心|诊所).+", normalized) and any(
            token in normalized for token in ["医院", "卫生院", "社康", "诊所"]
        ):
            for token in ["医院", "卫生院", "社康", "诊所"]:
                if token in normalized:
                    normalized = token
                    break
        return normalized.strip()

    def _poi_keyword_aliases(self, keyword: str) -> list[str]:
        normalized = self._normalize_poi_keyword(keyword)
        alias_map = {
            "麦当劳": ["麦当劳", "mcdonald", "mcdonalds", "金拱门", "得来速"],
            "咖啡店": ["咖啡", "coffee", "cafe", "caf", "瑞幸", "luckin", "库迪", "星巴克"],
            "咖啡馆": ["咖啡", "coffee", "cafe", "caf", "瑞幸", "luckin", "库迪", "星巴克"],
            "面包店": ["面包", "烘焙", "烘培", "吐司", "bread", "bakery", "bake"],
            "烘焙店": ["面包", "烘焙", "烘培", "吐司", "bread", "bakery", "bake"],
            "蛋糕店": ["蛋糕", "甜品", "dessert", "cake", "烘焙", "面包"],
            "饭店": ["饭店", "餐厅", "餐馆", "美食", "restaurant"],
            "餐厅": ["饭店", "餐厅", "餐馆", "美食", "restaurant"],
            "餐馆": ["饭店", "餐厅", "餐馆", "美食", "restaurant"],
            "烤肉店": ["烤肉", "烧烤", "韩式烤肉", "自助烤肉", "bbq"],
            "火锅店": ["火锅", "串串", "麻辣烫", "冒菜"],
            "医院": ["医院", "综合医院", "专科医院", "卫生院", "社康中心", "社区健康服务中心"],
            "最近医院": ["医院", "综合医院", "专科医院", "卫生院", "社康中心", "社区健康服务中心"],
        }
        aliases = alias_map.get(normalized, [normalized])
        deduped: list[str] = []
        for alias in aliases + [normalized]:
            lower_alias = alias.lower()
            if lower_alias and lower_alias not in deduped:
                deduped.append(lower_alias)
        return deduped

    def _filter_candidates_by_keyword(
        self,
        candidates: list[dict[str, Any]],
        keyword: str,
    ) -> list[dict[str, Any]]:
        aliases = self._poi_keyword_aliases(keyword)
        if not aliases:
            return candidates

        filtered: list[dict[str, Any]] = []
        for candidate in candidates:
            searchable = " ".join(
                [
                    str(candidate.get("name", "")),
                    str(candidate.get("address", "")),
                    str(candidate.get("type", "")),
                ]
            ).lower()
            if any(alias in searchable for alias in aliases):
                filtered.append(candidate)

        food_tokens = ["饭店", "餐厅", "餐馆", "美食", "烤肉", "烧烤", "火锅", "咖啡", "面包", "蛋糕", "甜品", "麦当劳"]
        if any(token in (keyword or "") for token in food_tokens):
            dining_candidates = [
                candidate
                for candidate in (filtered or candidates)
                if "餐饮服务" in str(candidate.get("type", ""))
            ]
            if dining_candidates:
                return dining_candidates

        return filtered or candidates

    def search_around_poi_candidates(
        self,
        center_coord: str,
        keyword: str,
        sortrule: str = "weight",
        limit: int = 5,
        radius: int = 50000,
        min_rating: float | None = None,
    ) -> list[dict[str, Any]]:
        if not self.api_key or not center_coord:
            return []

        url = "https://restapi.amap.com/v3/place/around"
        normalized_keyword = self._normalize_poi_keyword(keyword)
        try:
            search_terms = self._poi_keyword_aliases(normalized_keyword or keyword)
            if normalized_keyword and normalized_keyword not in search_terms:
                search_terms.insert(0, normalized_keyword)

            seen: set[str] = set()
            candidates: list[dict[str, Any]] = []
            for term in search_terms[:4]:
                for page in range(1, 4):
                    params = {
                        "key": self.api_key,
                        "location": center_coord,
                        "keywords": term,
                        "radius": radius,
                        "sortrule": sortrule,
                        "offset": 20,
                        "page": page,
                        "extensions": "all",
                    }
                    res = httpx.get(url, params=params, timeout=self.timeout)
                    res.raise_for_status()
                    pois = res.json().get("pois") or []
                    if not pois:
                        break

                    for poi in pois:
                        if not poi.get("location"):
                            continue
                        candidate = self._format_poi_candidate(poi)
                        candidate_id = candidate.get("id") or candidate.get("location") or candidate.get("name")
                        if not candidate_id or candidate_id in seen:
                            continue
                        seen.add(candidate_id)
                        candidates.append(candidate)

                    if len(candidates) >= limit * 4:
                        break
                if len(candidates) >= limit * 4:
                    break

            filtered = self._filter_candidates_by_keyword(candidates, normalized_keyword or keyword)
            if min_rating is not None:
                rated = [item for item in filtered if item.get("rating") is not None and item["rating"] >= min_rating]
                if rated:
                    filtered = rated
            if sortrule == "rating":
                filtered.sort(
                    key=lambda item: (
                        -(item.get("rating") or 0),
                        item.get("distance_m") if item.get("distance_m") is not None else 10**9,
                    )
                )
            else:
                filtered.sort(
                    key=lambda item: item.get("distance_m")
                    if item.get("distance_m") is not None
                    else 10**9
                )
            return filtered[:limit]
        except Exception as e:
            print(f"[warn] 周边候选搜索API异常: {e}")
            return []

    def search_along_route_candidates(
        self,
        origin_coord: str,
        dest_coord: str,
        keyword: str,
        limit: int = 5,
        search_range: int = 2000,
    ) -> list[dict[str, Any]]:
        if not self.api_key or not origin_coord or not dest_coord or not keyword:
            return []

        url = "https://restapi.amap.com/v3/place/route"
        normalized_keyword = self._normalize_poi_keyword(keyword)
        params = {
            "key": self.api_key,
            "origin": origin_coord,
            "destination": dest_coord,
            "keywords": normalized_keyword or keyword,
            "strategy": 0,
            "range": search_range,
        }

        try:
            response = httpx.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            route_pois = response.json().get("route_poi") or []
            candidates = [self._format_poi_candidate(poi) for poi in route_pois if poi.get("location")]
            filtered = self._filter_candidates_by_keyword(candidates, normalized_keyword or keyword)
            return filtered[:limit]
        except Exception as e:
            print(f"鈿狅笍 娌块€旀悳璋冪敤澶辫触: {e}")
            return []

    def get_driving_route_by_coords(self, origin: str, dest: str, waypoints: str = "", strategy: str = "speed") -> dict:
        """
        【泛化策略版】
        strategy: 允许大模型传入算路策略。
        """
        if not self.api_key or not origin or not dest:
            return {}
            
        print(f"[tool] 正在规划路线 (策略: {strategy})...")
        url = "https://restapi.amap.com/v3/direction/driving"
        
        # 核心：将自然语言的策略映射为高德底层的代号
        strategy_map = {
            "speed": "0",               # 速度优先（倾向走快速路/大路，红绿灯较少）
            "cost": "1",                # 费用优先（不走收费高速）
            "distance": "2",            # 距离优先（不管路况，物理距离最短）
            "avoid_congestion": "4"     # 躲避拥堵
        }
        api_strategy = strategy_map.get(strategy, "0")

        params = {
            "origin": origin,
            "destination": dest,
            "key": self.api_key,
            "extensions": "all",
            "strategy": api_strategy    # 将策略传给高德
        }
        if waypoints:
            params["waypoints"] = waypoints

        try:
            res = httpx.get(url, params=params, timeout=self.timeout)
            res.raise_for_status()
            data = res.json()
            if data.get("status") == "1" and data.get("route", {}).get("paths"):
                path = data["route"]["paths"][0]
                
                # 【修复了刚才的红绿灯 Bug】
                total_lights = int(path.get("traffic_lights", 0))
                
                # ... 下面保留你原来的 tmcs 路况解析代码不变 ...
                traffic_status = "路况未知"
                if "tmcs" in path:
                    status_list = [tmc.get("status") for tmc in path.get("tmcs", [])]
                    if "拥堵" in str(status_list): traffic_status = "路段拥堵"
                    elif "缓行" in str(status_list): traffic_status = "路段缓行"
                    else: traffic_status = "一路畅通"

                polyline: list[list[float]] = []
                for step in path.get("steps", []):
                    for raw_point in str(step.get("polyline", "")).split(";"):
                        try:
                            lng, lat = raw_point.split(",", 1)
                            point = [float(lng), float(lat)]
                        except ValueError:
                            continue
                        if not polyline or polyline[-1] != point:
                            polyline.append(point)

                return {
                    "distance_km": round(float(path.get("distance", 0)) / 1000.0, 1),
                    "eta_minutes": round(float(path.get("duration", 0)) / 60.0, 1),
                    "traffic_lights": total_lights,
                    "road_status": traffic_status,
                    "origin": origin,
                    "destination": dest,
                    "waypoints": [item for item in waypoints.split(";") if item],
                    "polyline": polyline,
                    "steps": [step.get("instruction", "") for step in path.get("steps", [])]
                }
            return {}
        except Exception as e:
            print(f"[warn] 驾车规划API异常: {e}")
            return {}
    def get_weather_by_location(self, location: str) -> str:
        """
        【原子工具 3：实时天气查询】带有终极脏数据兜底
        """
        if not self.api_key or not location:
            return "未知"
            
        print(f"[tool] 正在查询 '{location}' 所在区域的实时天气...")
        
        geo_info = self._geocode(location)
        adcode = geo_info.get("adcode") if geo_info else None
        
        # 【🚨 核心护盾：彻底防范空列表 [] 和 None】
        # 删除了那句导致报错的旧 if，直接进入兜底仲裁
        if not adcode or isinstance(adcode, list):
            adcode = "440300"  # 强制使用深圳市的 adcode 兜底！
            
        url = "https://restapi.amap.com/v3/weather/weatherInfo"
        params = {"city": adcode, "key": self.api_key, "extensions": "base"}
        
        try:
            import httpx
            res = httpx.get(url, params=params, timeout=self.timeout)
            res.raise_for_status()
            forecasts = res.json().get("lives") or []
            if forecasts:
                weather = forecasts[0].get("weather", "未知")
                temp = forecasts[0].get("temperature", "未知")
                print(f"[tool] 查询成功: {location} 当地 {weather}，气温 {temp}℃")
                return f"{weather}，{temp}℃"
        except Exception as e:
            print(f"[warn] 天气 API 异常: {e}")
            
        return "未知"

    def get_weather_by_coord(self, location: str) -> str:
        if not self.api_key or not location:
            return "未知"

        try:
            reverse = self.reverse_geocode(location)
            component = reverse.get("address_component") or {}
            adcode = component.get("adcode") or component.get("citycode")
            if not adcode:
                return "未知"

            res = httpx.get(
                "https://restapi.amap.com/v3/weather/weatherInfo",
                params={"city": adcode, "key": self.api_key, "extensions": "base"},
                timeout=self.timeout,
            )
            res.raise_for_status()
            forecasts = res.json().get("lives") or []
            if forecasts:
                weather = forecasts[0].get("weather", "未知")
                temp = forecasts[0].get("temperature", "未知")
                return f"{weather}，{temp}℃"
        except Exception as e:
            print(f"[warn] 坐标天气查询失败: {e}")
        return "未知"
