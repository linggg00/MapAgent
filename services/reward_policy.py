from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

from schemas.dag_model import (
    CreditAssignment,
    DagNode,
    EnvironmentSnapshot,
    ExecutionPlan,
    LearningTrace,
    NodeCredit,
    PlanningDAG,
    PolicySnapshot,
    RewardComponent,
    StrategyEvaluation,
    StrategyFeedbackResponse,
    ValidationReport,
)
from schemas.request import RoutePlanRequest


class RewardPolicyService:
    ISSUE_NODE_TYPE_MAP = {
        "missing_safety_check": ["safety_check"],
        "ev_resource_missing": ["resource_search"],
        "ev_range_not_enough": ["resource_search", "decision"],
        "indoor_charge_required": ["resource_search", "decision"],
        "closure_not_avoided": ["route_search", "decision"],
        "hazmat_control_missing": ["safety_check", "decision"],
        "speed_exceeded": ["safety_check", "decision"],
        "deadline_exceeded": ["route_search", "decision"],
        "bridge_weight_conflict": ["safety_check"],
        "height_conflict": ["safety_check"],
        "dag_missing_type": ["information_gathering", "constraint_extraction", "route_search", "output"],
        "dag_too_small": ["constraint_extraction", "route_search"],
        "dag_cycle": ["decision"],
    }

    COMPONENT_NODE_TYPE_MAP = {
        "feasibility": ["decision", "output"],
        "safety": ["safety_check", "decision"],
        "timeliness": ["route_search", "decision"],
        "efficiency": ["route_search", "decision"],
        "comfort": ["route_search", "decision"],
        "energy": ["resource_search", "decision"],
        "compliance": ["safety_check", "decision"],
        "reflection_efficiency": ["reflection", "decision"],
    }

    STAGE_NODE_TYPE_MAP = {
        "planning": {"information_gathering", "constraint_extraction", "route_search", "resource_search"},
        "decision": {"decision", "safety_check", "execution", "output"},
        "reflection": {"reflection"},
    }

    def __init__(self, store_path: str | Path | None = None) -> None:
        root = Path(__file__).resolve().parent.parent
        self.store_path = Path(store_path) if store_path else root / "runtime" / "policy_store.json"
        self._ensure_store()

    def derive_context_key(self, request: RoutePlanRequest, environment: EnvironmentSnapshot) -> str:
        if request.policy_context_key:
            return request.policy_context_key
        weather_bucket = self._bucketize_weather(environment.weather)
        urgency = "urgent" if request.arrival_deadline_minutes and request.arrival_deadline_minutes <= 120 else "normal"
        vehicle_bucket = request.vehicle_type
        goal_bucket = "+".join(sorted(request.goals)) if request.goals else "default"
        flags = []
        if request.cargo_type:
            flags.append("cargo")
        if environment.factbook.get("remaining_range_km") is not None:
            flags.append("low_range")
        if environment.restricted_zones:
            flags.append("restricted")
        return "|".join([vehicle_bucket, weather_bucket, urgency, goal_bucket, ",".join(flags) or "plain"])

    def snapshot(self, context_key: str) -> PolicySnapshot:
        store = self._load_store()
        context = store["contexts"].get(context_key, {})
        return PolicySnapshot(
            counts={key: int(value) for key, value in context.get("counts", {}).items()},
            value_estimates={key: float(value) for key, value in context.get("value_estimates", {}).items()},
        )

    def evaluate_candidate(
        self,
        request: RoutePlanRequest,
        environment: EnvironmentSnapshot,
        execution_plan: ExecutionPlan,
        validation: ValidationReport,
        strategy: str,
        policy_before: PolicySnapshot,
        round_index: int,
    ) -> StrategyEvaluation:
        components = self._build_reward_components(request, environment, execution_plan, validation, round_index)
        base_reward = round(sum(component.weighted_score for component in components), 2)

        policy_bias = 0.0
        exploration_bonus = 0.0
        if request.enable_learning:
            policy_bias = round(policy_before.value_estimates.get(strategy, 0.0) * 0.08, 2)
            total_visits = sum(policy_before.counts.values())
            strategy_visits = policy_before.counts.get(strategy, 0)
            exploration_bonus = round(math.sqrt(math.log(total_visits + 2) / (strategy_visits + 1)) * 2.5, 2)

        ranking_score = round(base_reward + policy_bias + exploration_bonus, 2)

        return StrategyEvaluation(
            strategy=strategy,
            base_reward=base_reward,
            ranking_score=ranking_score,
            policy_bias=policy_bias,
            exploration_bonus=exploration_bonus,
            validation_passed=validation.passed,
            issue_codes=[issue.code for issue in validation.issues],
            reward_components=components,
        )

    def assign_credit(
        self,
        dag: PlanningDAG,
        validation: ValidationReport,
        selected_evaluation: StrategyEvaluation,
    ) -> CreditAssignment:
        node_scores = {node.id: 0.15 for node in dag.nodes}
        node_reasons: dict[str, list[str]] = defaultdict(list)
        issue_backtrace: dict[str, list[str]] = {}

        for node in dag.nodes:
            if validation.passed:
                node_scores[node.id] += 0.1
                node_reasons[node.id].append("本轮执行通过主校验。")

        for component in selected_evaluation.reward_components:
            related_types = self.COMPONENT_NODE_TYPE_MAP.get(component.name, [])
            related_nodes = [node for node in dag.nodes if node.type in related_types]
            if not related_nodes:
                continue
            bonus = round(component.weighted_score / 100 * 0.8, 3)
            share = bonus / len(related_nodes)
            for node in related_nodes:
                node_scores[node.id] += share
                node_reasons[node.id].append(f"{component.name} 组件贡献 {component.weighted_score:.2f} 分。")

        for issue in validation.issues:
            impacted_types = self.ISSUE_NODE_TYPE_MAP.get(issue.code, ["decision"])
            impacted_nodes = [node for node in dag.nodes if node.type in impacted_types]
            if not impacted_nodes:
                impacted_nodes = [node for node in dag.nodes if node.type == "decision"] or dag.nodes[:1]
            penalty = 1.0 if issue.level == "error" else 0.35
            share = penalty / len(impacted_nodes)
            issue_backtrace[issue.code] = [node.id for node in impacted_nodes]
            for node in impacted_nodes:
                node_scores[node.id] -= share
                node_reasons[node.id].append(f"{issue.code}: {issue.message}")

        stage_scores = self._aggregate_stage_scores(dag.nodes, node_scores)
        ranked_nodes = [
            NodeCredit(
                node_id=node.id,
                node_name=node.name,
                score=round(node_scores[node.id], 3),
                reasons=node_reasons[node.id][:4],
            )
            for node in dag.nodes
        ]
        ranked_nodes.sort(key=lambda item: item.score, reverse=True)

        return CreditAssignment(
            stage_scores=stage_scores,
            node_scores=ranked_nodes,
            issue_backtrace=issue_backtrace,
        )

    def update_policy(
        self,
        context_key: str,
        strategy: str,
        reward: float,
        feedback_score: float | None = None,
    ) -> PolicySnapshot:
        store = self._load_store()
        contexts = store.setdefault("contexts", {})
        context = contexts.setdefault(context_key, {"counts": {}, "value_estimates": {}})
        counts = context.setdefault("counts", {})
        values = context.setdefault("value_estimates", {})

        adjusted_reward = reward
        if feedback_score is not None:
            adjusted_reward += feedback_score * 12.0

        count = int(counts.get(strategy, 0))
        previous = float(values.get(strategy, 0.0))
        updated = previous + (adjusted_reward - previous) / (count + 1)
        counts[strategy] = count + 1
        values[strategy] = round(updated, 4)
        self._save_store(store)
        return self.snapshot(context_key)

    def apply_feedback(
        self,
        context_key: str,
        strategy: str,
        feedback_score: float,
    ) -> StrategyFeedbackResponse:
        policy_after = self.update_policy(
            context_key=context_key,
            strategy=strategy,
            reward=0.0,
            feedback_score=feedback_score,
        )
        return StrategyFeedbackResponse(
            context_key=context_key,
            strategy=strategy,
            applied_reward_delta=round(feedback_score * 12.0, 2),
            policy_after=policy_after,
        )

    def build_learning_trace(
        self,
        enabled: bool,
        context_key: str,
        selected_evaluation: StrategyEvaluation,
        candidates: list[StrategyEvaluation],
        credit_assignment: CreditAssignment,
        policy_before: PolicySnapshot,
        policy_after: PolicySnapshot,
    ) -> LearningTrace:
        return LearningTrace(
            enabled=enabled,
            context_key=context_key,
            selected_strategy=selected_evaluation.strategy,
            base_reward=selected_evaluation.base_reward,
            policy_before=policy_before,
            policy_after=policy_after,
            candidates=sorted(candidates, key=lambda item: item.ranking_score, reverse=True),
            credit_assignment=credit_assignment,
        )

    def _build_reward_components(
        self,
        request: RoutePlanRequest,
        environment: EnvironmentSnapshot,
        execution_plan: ExecutionPlan,
        validation: ValidationReport,
        round_index: int,
    ) -> list[RewardComponent]:
        error_codes = {issue.code for issue in validation.issues if issue.level == "error"}
        warning_count = sum(1 for issue in validation.issues if issue.level == "warning")
        error_count = len(error_codes)

        feasibility_score = 1.0 if validation.passed else max(-1.0, 0.25 - 0.45 * error_count)

        safety_score = 1.0
        if "speed_exceeded" in error_codes:
            safety_score -= 0.8
        if {"bridge_weight_conflict", "height_conflict", "hazmat_control_missing"} & error_codes:
            safety_score -= 0.7
        safety_score -= min(0.1 * len(environment.warnings), 0.35)
        safety_score -= min(0.08 * warning_count, 0.2)
        safety_score = max(-1.0, min(1.0, safety_score))

        if request.arrival_deadline_minutes:
            if execution_plan.estimated_minutes <= request.arrival_deadline_minutes:
                timeliness_score = min(1.0, request.arrival_deadline_minutes / max(execution_plan.estimated_minutes, 1.0))
            else:
                over_ratio = (execution_plan.estimated_minutes - request.arrival_deadline_minutes) / request.arrival_deadline_minutes
                timeliness_score = max(-1.0, -over_ratio)
        else:
            timeliness_score = 0.7

        minimum_safe = validation.minimum_safe_minutes or execution_plan.estimated_minutes or 1.0
        efficiency_score = min(1.0, minimum_safe / max(execution_plan.estimated_minutes, 1.0) * 1.15)

        comfort_relevant = request.passengers.elderly > 0 or request.passengers.children > 0 or environment.factbook.get("prefer_smooth_drive")
        if comfort_relevant:
            max_speed = max((step.speed_limit_kmh for step in execution_plan.route_steps), default=0.0)
            comfort_score = 1.0
            if max_speed > 80:
                comfort_score -= 0.4
            if len(execution_plan.route_steps) > 3:
                comfort_score -= 0.2
            if "comfort_warning" in error_codes:
                comfort_score -= 0.2
        else:
            comfort_score = 0.6
        comfort_score = max(-1.0, min(1.0, comfort_score))

        if request.vehicle_type == "ev":
            has_charge = any("快充" in step.to_name or "补能" in step.instruction for step in execution_plan.route_steps)
            range_km = environment.factbook.get("remaining_range_km")
            energy_score = 0.9 if has_charge else 0.35
            if range_km is not None and range_km >= execution_plan.estimated_distance_km:
                energy_score = 1.0
            if {"ev_range_not_enough", "indoor_charge_required"} & error_codes:
                energy_score = -0.6
        else:
            energy_score = 0.55

        compliance_relevant = request.vehicle_type in {"truck", "hazmat_truck"} or bool(request.cargo_type)
        if compliance_relevant:
            compliance_score = 0.95 if not ({"hazmat_control_missing", "bridge_weight_conflict", "height_conflict"} & error_codes) else -0.75
        else:
            compliance_score = 0.6

        reflection_efficiency_score = max(-1.0, 1.0 - round_index * 0.35)

        component_specs = [
            ("feasibility", 24.0, feasibility_score, "是否形成可通过校验的可执行方案。"),
            ("safety", 22.0, safety_score, "天气、限速、危化品和基础安全约束。"),
            ("timeliness", 16.0, timeliness_score, "是否满足时限约束以及到达效率。"),
            ("efficiency", 10.0, efficiency_score, "方案距离与时间是否接近物理可行下界。"),
            ("comfort", 8.0, comfort_score, "对老人、儿童或平稳驾驶需求的满足程度。"),
            ("energy", 8.0, energy_score, "续航与补能闭环是否可靠。"),
            ("compliance", 8.0, compliance_score, "限高、限重、危化品等合规性。"),
            ("reflection_efficiency", 4.0, reflection_efficiency_score, "反思轮数越多，学习代价越高。"),
        ]

        components = []
        for name, weight, score, reason in component_specs:
            components.append(
                RewardComponent(
                    name=name,
                    weight=weight,
                    score=round(score, 4),
                    weighted_score=round(weight * score, 4),
                    reason=reason,
                )
            )
        return components

    def _aggregate_stage_scores(self, nodes: list[DagNode], node_scores: dict[str, float]) -> dict[str, float]:
        stage_scores: dict[str, float] = {}
        for stage_name, node_types in self.STAGE_NODE_TYPE_MAP.items():
            relevant = [node_scores[node.id] for node in nodes if node.type in node_types]
            stage_scores[stage_name] = round(sum(relevant), 3) if relevant else 0.0
        return stage_scores

    def _bucketize_weather(self, weather: str) -> str:
        if any(keyword in weather for keyword in ["暴雪", "结冰", "冻雨"]):
            return "snow_ice"
        if any(keyword in weather for keyword in ["暴雨", "台风", "强风"]):
            return "storm"
        if any(keyword in weather for keyword in ["灾害", "地震", "泥石流"]):
            return "disaster"
        if any(keyword in weather for keyword in ["雾", "低能见度"]):
            return "fog"
        return "normal"

    def _ensure_store(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.store_path.exists():
            self._save_store({"contexts": {}})

    def _load_store(self) -> dict:
        self._ensure_store()
        return json.loads(self.store_path.read_text(encoding="utf-8"))

    def _save_store(self, payload: dict) -> None:
        self.store_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
