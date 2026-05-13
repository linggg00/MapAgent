import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pipelines.agent_core import PDRAgentCore
from schemas.dag_model import DagEdge, DagNode, EnvironmentSnapshot, ExecutionPlan, PlanningDAG, RouteSegment
from schemas.request import RoutePlanRequest
from services.amap_client import AmapClient
from services.reward_policy import RewardPolicyService


class FakeAmapClient:
    def get_environment_snapshot(self, request: RoutePlanRequest) -> EnvironmentSnapshot:
        return EnvironmentSnapshot(
            source="mock",
            weather="晴",
            speed_cap_kmh=60,
            factbook={},
        )

    def search_around_poi_candidates(
        self,
        center_coord: str,
        keyword: str,
        sortrule: str = "weight",
        limit: int = 5,
    ) -> list[dict]:
        if keyword == "麦当劳":
            return [
                {"id": "m1", "name": "麦当劳(科苑路店)", "address": "科苑路1号", "location": "113.1,22.1", "distance_m": 320, "type": "快餐厅"},
                {"id": "m2", "name": "麦当劳(软件园店)", "address": "软件园南街8号", "location": "113.2,22.2", "distance_m": 580, "type": "快餐厅"},
            ]
        if keyword == "公共厕所":
            return [
                {"id": "w1", "name": "公共厕所(科技园南区)", "address": "科技园南区入口旁", "location": "113.3,22.3", "distance_m": 180, "type": "公共厕所"},
                {"id": "w2", "name": "公共厕所(创园路口)", "address": "创园路与科发路交叉口", "location": "113.4,22.4", "distance_m": 260, "type": "公共厕所"},
            ]
        if keyword == "烤肉店":
            return [
                {"id": "k1", "name": "韩式烤肉店(科苑路店)", "address": "科苑路18号", "location": "113.31,22.31", "distance_m": 220, "type": "烤肉店"},
                {"id": "k2", "name": "自助烤肉店(软件园店)", "address": "软件园中路66号", "location": "113.32,22.32", "distance_m": 360, "type": "烤肉店"},
            ]
        return []

    def search_along_route_candidates(
        self,
        origin_coord: str,
        dest_coord: str,
        keyword: str,
        limit: int = 5,
        search_range: int = 2000,
    ) -> list[dict]:
        return self.search_around_poi_candidates(origin_coord, keyword, "distance", limit)

    def build_execution_plan(
        self,
        request: RoutePlanRequest,
        environment: EnvironmentSnapshot,
        dag: PlanningDAG,
        reflection_context: str | None = None,
        strategy_override: str | None = None,
    ) -> ExecutionPlan:
        assert request.extra["resolved_destination"]["name"] == "麦当劳(科苑路店)"
        assert request.extra["resolved_waypoints"][0]["name"] == "公共厕所(科技园南区)"
        return ExecutionPlan(
            mode="driving",
            strategy="balanced",
            route_steps=[
                RouteSegment(
                    from_name="当前位置",
                    to_name="麦当劳(科苑路店)",
                    mode="driving",
                    instruction="从当前位置出发，途中经过公共厕所(科技园南区)，然后到达麦当劳(科苑路店)",
                    distance_km=3.2,
                    eta_minutes=12,
                    speed_limit_kmh=40,
                )
            ],
            estimated_distance_km=3.2,
            estimated_minutes=12,
            average_speed_kmh=16,
            checks=[],
        )

    def _is_coord(self, text: str) -> bool:
        return "," in text

    def _ensure_coord(self, place: str) -> str:
        return place


class FakeLLMClient:
    def generate_planning_dag(
        self,
        request: RoutePlanRequest,
        environment: EnvironmentSnapshot,
        reflection_context: str | None = None,
    ) -> PlanningDAG:
        return PlanningDAG(
            nodes=[
                DagNode(id="n1", name="采集环境", type="information_gathering"),
                DagNode(id="n2", name="提取约束", type="constraint_extraction"),
                DagNode(id="n3", name="规划路线", type="route_search"),
                DagNode(id="n4", name="输出结果", type="output"),
            ],
            edges=[
                DagEdge(source="n1", target="n2"),
                DagEdge(source="n2", target="n3"),
                DagEdge(source="n3", target="n4"),
            ],
            summary="测试DAG",
        )

    def generate_narration(
        self,
        request: RoutePlanRequest,
        environment: EnvironmentSnapshot,
        execution_plan: ExecutionPlan,
        validation,
    ) -> str:
        return "测试播报"


class PDRAgentCoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        policy_store = Path(self.temp_dir.name) / "policy_store.json"
        self.agent = PDRAgentCore(reward_policy=RewardPolicyService(policy_store))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_ev_scene_can_pass_with_indoor_charging(self) -> None:
        request = RoutePlanRequest(
            start="东北城区",
            destination="城郊老人家",
            scene="-35度暴雪，高架桥结冰封闭",
            user_request="旧款纯电车剩余续航40km，需要去看望突发疾病老人，沿途必须经过室内恒温快充站",
            vehicle_type="ev",
            hard_constraints=["必须室内快充", "避开结冰高架"],
            use_mock_data=True,
        )
        response = self.agent.execute(request)
        self.assertEqual(response.status, "success")
        self.assertTrue(response.validation.passed)
        self.assertTrue(any("室内" in step.to_name for step in response.execution_plan.route_steps))
        self.assertTrue(response.learning_trace)
        self.assertEqual(response.learning_trace[0].selected_strategy, response.execution_plan.strategy)

    def test_impossible_deadline_is_rejected(self) -> None:
        request = RoutePlanRequest(
            start="北京顺义区首都机场T3航站楼",
            destination="张家口崇礼云顶滑雪场",
            scene="暴雪天气，部分高速封闭",
            user_request="30分钟内赶到目的地，生成安全路线规划任务DAG",
            vehicle_type="sedan",
            arrival_deadline_minutes=30,
            hard_constraints=["避开封闭高速", "不能超过当前天气安全限速"],
            use_mock_data=True,
        )
        response = self.agent.execute(request)
        self.assertEqual(response.status, "failed")
        self.assertIn("deadline_exceeded", [issue.code for issue in response.validation.issues])
        self.assertGreaterEqual(len(response.learning_trace[0].candidates), 2)

    def test_hazmat_scene_includes_compliance_checks(self) -> None:
        request = RoutePlanRequest(
            start="化工园区",
            destination="海岛工业区",
            scene="跨海大桥封闭，危化品车辆必须避开居民区和水源保护地，3小时内到达",
            user_request="载有液氯的重型卡车需要绝对避开居民区和水源保护地",
            vehicle_type="hazmat_truck",
            cargo_type="液氯",
            arrival_deadline_minutes=180,
            use_mock_data=True,
        )
        response = self.agent.execute(request)
        self.assertEqual(response.status, "success")
        self.assertTrue(any("危化品合规" in check for check in response.execution_plan.checks))
        self.assertIn("planning", response.learning_trace[0].credit_assignment.stage_scores)

    def test_ambiguous_destination_and_waypoint_requires_clarification(self) -> None:
        agent = PDRAgentCore(
            amap_client=FakeAmapClient(),
            llm_client=FakeLLMClient(),
            reward_policy=RewardPolicyService(Path(self.temp_dir.name) / "clarify_policy_store.json"),
        )
        request = RoutePlanRequest(
            query="我想去麦当劳，途经公共厕所",
            start="113.90,22.50",
            use_mock_data=True,
            need_narration=False,
            extra={"current_gps": "113.90,22.50"},
        )
        response = agent.execute(request)
        self.assertEqual(response.status, "needs_clarification")
        self.assertIsNotNone(response.clarification)
        self.assertEqual([slot.slot_id for slot in response.clarification.pending_slots], ["destination"])

    def test_selected_destination_and_waypoint_can_continue_planning(self) -> None:
        agent = PDRAgentCore(
            amap_client=FakeAmapClient(),
            llm_client=FakeLLMClient(),
            reward_policy=RewardPolicyService(Path(self.temp_dir.name) / "resolved_policy_store.json"),
        )
        request = RoutePlanRequest(
            query="我想去麦当劳，途经公共厕所",
            start="113.90,22.50",
            use_mock_data=True,
            need_narration=False,
            extra={
                "current_gps": "113.90,22.50",
                "poi_selections": {
                    "destination": 1,
                    "waypoint_1": 1,
                },
            },
        )
        response = agent.execute(request)
        self.assertEqual(response.status, "success")
        self.assertEqual(response.execution_plan.route_steps[0].to_name, "麦当劳(科苑路店)")

    def test_extract_route_entities_supports_yao_without_qu(self) -> None:
        agent = PDRAgentCore(amap_client=FakeAmapClient(), llm_client=FakeLLMClient())
        destination, waypoints = agent._extract_route_entities("我要麦当劳，顺便要途经公共厕所")
        self.assertEqual(destination, "麦当劳")
        self.assertEqual(waypoints, ["公共厕所"])

    def test_generic_waypoint_like_roast_meat_shop_requires_selection(self) -> None:
        agent = PDRAgentCore(
            amap_client=FakeAmapClient(),
            llm_client=FakeLLMClient(),
            reward_policy=RewardPolicyService(Path(self.temp_dir.name) / "waypoint_policy_store.json"),
        )
        request = RoutePlanRequest(
            query="我要麦当劳，顺便要途经烤肉店",
            start="113.90,22.50",
            use_mock_data=True,
            need_narration=False,
            extra={
                "current_gps": "113.90,22.50",
                "poi_selections": {"destination": 1},
            },
        )
        response = agent.execute(request)
        self.assertEqual(response.status, "needs_clarification")
        self.assertEqual([slot.slot_id for slot in response.clarification.pending_slots], ["waypoint_1"])

    def test_plan_route_without_end_uses_last_waypoint_as_end(self) -> None:
        agent = PDRAgentCore()
        start_coord, end_coord, waypoints = agent._repair_route_endpoint_from_waypoints(
            start_coord="current_gps",
            end_coord="",
            waypoints="113.1,22.1;113.2,22.2",
            fallback_end="113.9,22.9",
            current_gps="113.0,22.0",
        )
        self.assertEqual(start_coord, "current_gps")
        self.assertEqual(end_coord, "113.2,22.2")
        self.assertEqual(waypoints, "113.1,22.1")

    def test_filter_candidates_by_keyword_excludes_unrelated_coffee_for_bakery(self) -> None:
        amap = AmapClient()
        candidates = [
            {"name": "库迪咖啡(中山大学深圳校区店)", "address": "校区东园超市内", "type": "咖啡厅"},
            {"name": "九越烘焙", "address": "圳美二路", "type": "面包甜点"},
            {"name": "面包好了", "address": "公常路", "type": "面包店"},
        ]
        filtered = amap._filter_candidates_by_keyword(candidates, "面包店")
        self.assertEqual([item["name"] for item in filtered], ["九越烘焙", "面包好了"])

    def test_normalize_poi_keyword_removes_nearby_prefix(self) -> None:
        amap = AmapClient()
        self.assertEqual(amap._normalize_poi_keyword("附近咖啡店"), "咖啡店")

    def test_looks_ambiguous_treats_nearby_mcdonalds_as_generic(self) -> None:
        agent = PDRAgentCore()
        candidates = [
            {"name": "McDonald's", "address": "A"},
            {"name": "麦当劳(软件园店)", "address": "B"},
        ]
        self.assertTrue(agent._looks_ambiguous("附近麦当劳", candidates))


if __name__ == "__main__":
    unittest.main()
