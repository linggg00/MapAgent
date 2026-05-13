from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from schemas.request import RoutePlanRequest


NodeType = Literal[
    "information_gathering",
    "constraint_extraction",
    "route_search",
    "resource_search",
    "decision",
    "safety_check",
    "execution",
    "output",
    "reflection",
]

IssueLevel = Literal["warning", "error"]

class RouteStep(BaseModel):
    """用于存储真实高德导航的每一个具体的转向步骤"""
    instruction: str = Field(default="", description="具体的导航指令，如'向左前方行走'")
    distance_km: float = Field(default=0.0, description="该路段距离（公里）")
    eta_minutes: float = Field(default=0.0, description="该路段预估耗时（分钟）")
    from_name: str = Field(default="", description="起点或当前路名")
    to_name: str = Field(default="", description="终点或动作")
class DagNode(BaseModel):
    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    type: NodeType
    reasoning: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DagEdge(BaseModel):
    source: str
    target: str
    label: str | None = None


class PlanningDAG(BaseModel):
    nodes: list[DagNode] = Field(default_factory=list)
    edges: list[DagEdge] = Field(default_factory=list)
    summary: str | None = None

    @model_validator(mode="after")
    def validate_graph(self) -> "PlanningDAG":
        node_ids = {node.id for node in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ValueError("DAG 节点 id 必须唯一")
        for edge in self.edges:
            if edge.source not in node_ids or edge.target not in node_ids:
                raise ValueError("DAG 边必须引用已存在的节点")
        return self


class ValidationIssue(BaseModel):
    code: str
    level: IssueLevel = "error"
    message: str
    suggestion: str | None = None


class ValidationReport(BaseModel):
    passed: bool
    summary: str
    issues: list[ValidationIssue] = Field(default_factory=list)
    speed_cap_kmh: float | None = None
    minimum_safe_minutes: float | None = None


class RewardComponent(BaseModel):
    name: str
    weight: float
    score: float
    weighted_score: float
    reason: str


class StrategyEvaluation(BaseModel):
    strategy: str
    base_reward: float
    ranking_score: float
    policy_bias: float = 0.0
    exploration_bonus: float = 0.0
    validation_passed: bool
    issue_codes: list[str] = Field(default_factory=list)
    reward_components: list[RewardComponent] = Field(default_factory=list)


class PolicySnapshot(BaseModel):
    counts: dict[str, int] = Field(default_factory=dict)
    value_estimates: dict[str, float] = Field(default_factory=dict)


class NodeCredit(BaseModel):
    node_id: str
    node_name: str
    score: float
    reasons: list[str] = Field(default_factory=list)


class CreditAssignment(BaseModel):
    stage_scores: dict[str, float] = Field(default_factory=dict)
    node_scores: list[NodeCredit] = Field(default_factory=list)
    issue_backtrace: dict[str, list[str]] = Field(default_factory=dict)


class LearningTrace(BaseModel):
    enabled: bool = True
    context_key: str
    selected_strategy: str
    base_reward: float
    policy_before: PolicySnapshot = Field(default_factory=PolicySnapshot)
    policy_after: PolicySnapshot = Field(default_factory=PolicySnapshot)
    candidates: list[StrategyEvaluation] = Field(default_factory=list)
    credit_assignment: CreditAssignment = Field(default_factory=CreditAssignment)


class POICandidate(BaseModel):
    id: str | None = None
    name: str
    address: str = ""
    location: str
    distance_m: int | None = None
    type: str | None = None


class ClarificationSlot(BaseModel):
    slot_id: str
    slot_type: Literal["destination", "waypoint"]
    keyword: str
    question: str
    candidates: list[POICandidate] = Field(default_factory=list)
    required: bool = True


class RouteClarification(BaseModel):
    prompt: str
    pending_slots: list[ClarificationSlot] = Field(default_factory=list)
    resolved_slots: dict[str, POICandidate] = Field(default_factory=dict)


class RouteSegment(BaseModel):
    from_name: str
    to_name: str
    mode: str
    instruction: str
    distance_km: float = Field(ge=0)
    eta_minutes: float = Field(ge=0)
    speed_limit_kmh: float = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)


class ExecutionPlan(BaseModel):
    mode: str
    strategy: str
    route_steps: list[RouteStep] = Field(default_factory=list) 
    estimated_distance_km: float = Field(ge=0)
    estimated_minutes: float = Field(ge=0)
    average_speed_kmh: float = Field(ge=0)
    estimated_cost: float | None = Field(default=None, ge=0)
    route_link: str | None = None
    route_steps: list[RouteSegment] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)


class EnvironmentSnapshot(BaseModel):
    source: Literal["mock", "amap"] = "mock"
    weather: str
    temperature_c: float | None = None
    visibility_km: float | None = None
    speed_cap_kmh: float = Field(default=80, ge=1)
    closed_roads: list[str] = Field(default_factory=list)
    restricted_zones: list[str] = Field(default_factory=list)
    available_facilities: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    factbook: dict[str, Any] = Field(default_factory=dict)


class ReflectionRound(BaseModel):
    round_index: int = Field(ge=0)
    planner_summary: str
    validation: ValidationReport
    revised: bool = False
    selected_strategy: str | None = None
    base_reward: float | None = None


class RoutePlanResponse(BaseModel):
    status: Literal["success", "failed", "needs_revision", "needs_clarification"]
    request_echo: RoutePlanRequest
    environment: EnvironmentSnapshot | None = None
    dag: PlanningDAG | None = None
    execution_plan: ExecutionPlan | None = None
    validation: ValidationReport | None = None
    narration: str | None = None
    clarification: RouteClarification | None = None
    reflection_trace: list[ReflectionRound] = Field(default_factory=list)
    learning_trace: list[LearningTrace] = Field(default_factory=list)


class StrategyFeedbackResponse(BaseModel):
    context_key: str
    strategy: str
    applied_reward_delta: float
    policy_after: PolicySnapshot
