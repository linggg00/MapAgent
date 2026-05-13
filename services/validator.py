from __future__ import annotations

from collections import defaultdict, deque

from schemas.dag_model import (
    EnvironmentSnapshot,
    ExecutionPlan,
    PlanningDAG,
    ValidationIssue,
    ValidationReport,
)
from schemas.request import RoutePlanRequest


class Validator:
    def validate(
        self,
        request: RoutePlanRequest,
        dag: PlanningDAG,
        execution_plan: ExecutionPlan,
        environment: EnvironmentSnapshot,
    ) -> ValidationReport:
        issues: list[ValidationIssue] = []
        issues.extend(self._validate_dag_structure(dag))
        issues.extend(self._validate_domain_coverage(request, dag, environment))
        issues.extend(self._validate_execution_plan(request, execution_plan, environment))

        has_error = any(issue.level == "error" for issue in issues)
        summary = "规划满足当前物理和业务约束。"
        if has_error:
            summary = "规划仍存在不可忽略的物理或业务冲突，需要反思重试。"
        elif issues:
            summary = "规划基本可执行，但有若干风险提示需要对前端明确展示。"

        minimum_safe_minutes = 0.0
        if execution_plan.estimated_distance_km > 0 and environment.speed_cap_kmh > 0:
            minimum_safe_minutes = execution_plan.estimated_distance_km / environment.speed_cap_kmh * 60

        return ValidationReport(
            passed=not has_error,
            summary=summary,
            issues=issues,
            speed_cap_kmh=environment.speed_cap_kmh,
            minimum_safe_minutes=round(minimum_safe_minutes, 1),
        )

    def build_reflection_context(self, report: ValidationReport) -> str:
        if report.passed:
            return "无"
        fragments = [f"[{issue.code}] {issue.message}" for issue in report.issues if issue.level == "error"]
        suggestions = [issue.suggestion for issue in report.issues if issue.suggestion]
        joined = "；".join(fragments + suggestions)
        return joined or "上一轮校验失败，请重新规划。"

    def _validate_dag_structure(self, dag: PlanningDAG) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        node_types = {node.type for node in dag.nodes}
        required_types = {"information_gathering", "constraint_extraction", "route_search", "output"}
        for required in required_types:
            if required not in node_types:
                issues.append(
                    ValidationIssue(
                        code="dag_missing_type",
                        message=f"DAG 缺少关键节点类型：{required}",
                        suggestion="补全环境采集、约束提取、候选路线搜索和输出节点。",
                    )
                )

        if len(dag.nodes) < 4:
            issues.append(
                ValidationIssue(
                    code="dag_too_small",
                    message="DAG 节点过少，无法覆盖规划闭环。",
                    suggestion="至少保留采集、提取、搜索、输出四类节点。",
                )
            )

        if self._has_cycle(dag):
            issues.append(
                ValidationIssue(
                    code="dag_cycle",
                    message="DAG 含有环，无法作为可执行任务图。",
                    suggestion="移除回环边，保持单向依赖关系。",
                )
            )
        return issues

    def _validate_domain_coverage(
        self,
        request: RoutePlanRequest,
        dag: PlanningDAG,
        environment: EnvironmentSnapshot,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        node_text = " ".join(f"{node.name} {node.reasoning or ''}" for node in dag.nodes)

        if environment.warnings and "safety_check" not in {node.type for node in dag.nodes}:
            issues.append(
                ValidationIssue(
                    code="missing_safety_check",
                    message="环境存在高风险信息，但 DAG 中没有显式安全校验节点。",
                    suggestion="为恶劣天气、灾害或封控场景加入 safety_check 节点。",
                )
            )

        if request.vehicle_type == "ev" or environment.factbook.get("remaining_range_km") is not None:
            if "补能" not in node_text and "续航" not in node_text and "resource_search" not in {node.type for node in dag.nodes}:
                issues.append(
                    ValidationIssue(
                        code="ev_resource_missing",
                        message="电动车或低续航场景缺少补能资源搜索节点。",
                        suggestion="加入室内快充站、可达半径或续航校验节点。",
                    )
                )

        if request.vehicle_type in {"truck", "hazmat_truck"} or request.cargo_type:
            if "限重" not in node_text and "限高" not in node_text and "危化品" not in node_text:
                issues.append(
                    ValidationIssue(
                        code="compliance_missing",
                        message="特殊车辆场景缺少限重、限高或危化品合规检查。",
                        suggestion="在 DAG 中加入承载、净空和禁行区域校验。",
                    )
                )
        return issues

    def _validate_execution_plan(
        self,
        request: RoutePlanRequest,
        execution_plan: ExecutionPlan,
        environment: EnvironmentSnapshot,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        facts = environment.factbook

        if execution_plan.average_speed_kmh > environment.speed_cap_kmh * 1.05:
            issues.append(
                ValidationIssue(
                    code="speed_exceeded",
                    message=(
                        f"规划平均速度 {execution_plan.average_speed_kmh:.1f} km/h 超过环境安全上限 "
                        f"{environment.speed_cap_kmh:.1f} km/h。"
                    ),
                    suggestion="降低时效目标，或改为更安全的执行策略。",
                )
            )

        if request.arrival_deadline_minutes is not None:
            if execution_plan.estimated_minutes > request.arrival_deadline_minutes:
                issues.append(
                    ValidationIssue(
                        code="deadline_exceeded",
                        message=(
                            f"预计耗时 {execution_plan.estimated_minutes:.1f} 分钟，超过时限 "
                            f"{request.arrival_deadline_minutes} 分钟。"
                        ),
                        suggestion="重新搜索更短方案，或直接告知前端当前约束下无法准时到达。",
                    )
                )

        remaining_range = facts.get("remaining_range_km")
        if request.vehicle_type == "ev" and remaining_range is not None:
            has_charging = any("快充" in step.to_name or "补能" in step.instruction for step in execution_plan.route_steps)
            if remaining_range < execution_plan.estimated_distance_km and not has_charging:
                issues.append(
                    ValidationIssue(
                        code="ev_range_not_enough",
                        message="续航不足以支撑当前路线，且执行计划中缺少补能节点。",
                        suggestion="加入室内快充站或缩短路线距离。",
                    )
                )
            if facts.get("need_indoor_charge") and not any("室内" in step.to_name for step in execution_plan.route_steps):
                issues.append(
                    ValidationIssue(
                        code="indoor_charge_required",
                        message="场景要求室内恒温快充，但执行计划未命中对应补能点。",
                        suggestion="优先选择室内可用快充站作为中转点。",
                    )
                )

        if environment.closed_roads and not any("绕行" in step.instruction for step in execution_plan.route_steps):
            issues.append(
                ValidationIssue(
                    code="closure_not_avoided",
                    message="环境中存在封闭路段，但执行计划未明确体现绕行。",
                    suggestion="在步骤中加入绕行节点，或重新规划主通道。",
                )
            )

        if request.vehicle_type == "hazmat_truck" and "危化品" not in " ".join(execution_plan.checks):
            issues.append(
                ValidationIssue(
                    code="hazmat_control_missing",
                    message="危化品运输方案未体现敏感区域规避与合规检查。",
                    suggestion="补充危化品检查站、禁行区规避或专用通道说明。",
                )
            )

        if facts.get("bridge_weight_limit_tons") and facts.get("vehicle_weight_tons"):
            if facts["vehicle_weight_tons"] > facts["bridge_weight_limit_tons"]:
                issues.append(
                    ValidationIssue(
                        code="bridge_weight_conflict",
                        message=(
                            f"车辆重量 {facts['vehicle_weight_tons']} 吨超过限重 "
                            f"{facts['bridge_weight_limit_tons']} 吨。"
                        ),
                        suggestion="必须绕开限重桥梁，不能继续沿当前方案执行。",
                    )
                )

        if facts.get("height_limit_m") and facts.get("vehicle_height_m"):
            if facts["vehicle_height_m"] > facts["height_limit_m"]:
                issues.append(
                    ValidationIssue(
                        code="height_conflict",
                        message=(
                            f"车辆高度 {facts['vehicle_height_m']} 米超过限高 "
                            f"{facts['height_limit_m']} 米。"
                        ),
                        suggestion="改走满足净空要求的通道，或从货运入口进入。",
                    )
                )

        if facts.get("prefer_smooth_drive"):
            fast_segments = [step for step in execution_plan.route_steps if step.speed_limit_kmh > 85]
            if fast_segments:
                issues.append(
                    ValidationIssue(
                        code="comfort_warning",
                        level="warning",
                        message="当前方案有较快路段，老人/儿童乘坐舒适度可能不足。",
                        suggestion="前端展示时应强调平稳驾驶与保守跟车距离。",
                    )
                )
        return issues

    def _has_cycle(self, dag: PlanningDAG) -> bool:
        indegree: dict[str, int] = {node.id: 0 for node in dag.nodes}
        graph: dict[str, list[str]] = defaultdict(list)
        for edge in dag.edges:
            graph[edge.source].append(edge.target)
            indegree[edge.target] += 1

        queue = deque(node_id for node_id, degree in indegree.items() if degree == 0)
        visited = 0
        while queue:
            node_id = queue.popleft()
            visited += 1
            for neighbor in graph[node_id]:
                indegree[neighbor] -= 1
                if indegree[neighbor] == 0:
                    queue.append(neighbor)
        return visited != len(dag.nodes)
