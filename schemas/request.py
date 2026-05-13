from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


VehicleType = Literal[
    "sedan",
    "suv",
    "truck",
    "hazmat_truck",
    "ambulance",
    "rescue_vehicle",
    "ev",
    "public_transit",
]

TravelMode = Literal["driving", "truck", "walking", "cycling", "transit", "multimodal"]

PlanningGoal = Literal[
    "fastest",
    "safest",
    "cheapest",
    "smoothest",
    "energy_saving",
    "rescue_priority",
]


class PassengerProfile(BaseModel):
    elderly: int = Field(default=0, ge=0)
    children: int = Field(default=0, ge=0)
    injured: int = Field(default=0, ge=0)


class RoutePlanRequest(BaseModel):
    # 【新增修改点 1】专门接收用户输入的自然语言原话，直接喂给你微调后的大模型
    query: str = Field(default="", description="用户自然语言直接输入")

    # 【新增修改点 2】去掉原本的 `...` 必填限制，改为 default="默认起点"，防止 Pydantic 报错拦截
    start: str = Field(default="默认起点", description="出发地")
    destination: str = Field(default="默认终点", description="目的地")
    
    scene: str = Field(default="", description="环境、天气、突发事件等上下文")
    user_request: str = Field(default="", description="用户自然语言原始需求")
    vehicle_type: VehicleType = Field(default="sedan")
    cargo_type: str | None = Field(default=None, description="货物类型，如液氯、医疗氧气等")
    departure_time: str | None = Field(default=None, description="期望出发时间")
    arrival_deadline_minutes: int | None = Field(default=None, ge=1, le=1440)
    prefer_modes: list[TravelMode] = Field(default_factory=lambda: ["driving"])
    goals: list[PlanningGoal] = Field(default_factory=list) # 放宽了 goals 的默认限制
    hard_constraints: list[str] = Field(default_factory=list)
    soft_constraints: list[str] = Field(default_factory=list)
    passengers: PassengerProfile = Field(default_factory=PassengerProfile)
    budget_limit: float | None = Field(default=None, ge=0)
    need_narration: bool = Field(default=True)
    use_mock_data: bool = Field(default=True, description="无高德 key 时自动回退到 mock 数据")
    max_reflection_rounds: int = Field(default=2, ge=0, le=5)
    enable_learning: bool = Field(default=True, description="是否启用强化学习式策略选择与更新")
    policy_context_key: str | None = Field(default=None, description="可选的策略学习上下文 key")
    user_feedback_score: float | None = Field(default=None, ge=-1.0, le=1.0, description="可选用户反馈分数")
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_fields(self) -> "RoutePlanRequest":
        # 【新增修改点 3】增加对 query 的 strip 处理
        self.query = self.query.strip()
        self.start = self.start.strip()
        self.destination = self.destination.strip()
        self.scene = self.scene.strip()
        self.user_request = self.user_request.strip()
        self.hard_constraints = [item.strip() for item in self.hard_constraints if item.strip()]
        self.soft_constraints = [item.strip() for item in self.soft_constraints if item.strip()]
        
        # 如果 user_request 是空的，优先用 query 顶替
        if not self.user_request and self.query:
            self.user_request = self.query
            
        if not self.user_request:
            fragments = [
                f"从{self.start}前往{self.destination}",
                self.scene,
                "，".join(self.hard_constraints) if self.hard_constraints else "",
                "，".join(self.soft_constraints) if self.soft_constraints else "",
            ]
            self.user_request = "；".join(item for item in fragments if item)
        return self

    def merged_context(self) -> str:
        parts = [
            f"自然语言输入：{self.query}" if self.query else "",
            f"起点：{self.start}",
            f"终点：{self.destination}",
            f"车辆：{self.vehicle_type}",
            f"场景：{self.scene}" if self.scene else "",
            f"硬约束：{'；'.join(self.hard_constraints)}" if self.hard_constraints else "",
            f"软约束：{'；'.join(self.soft_constraints)}" if self.soft_constraints else "",
            f"用户描述：{self.user_request}" if self.user_request else "",
        ]
        return "\n".join(item for item in parts if item)


class StrategyFeedbackRequest(BaseModel):
    context_key: str = Field(..., min_length=1)
    strategy: str = Field(..., min_length=1)
    feedback_score: float = Field(..., ge=-1.0, le=1.0)
    comment: str | None = Field(default=None, max_length=500)