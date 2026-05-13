# 数据校验层 (Schemas Layer)

这里定义了整个系统的输入输出契约：

- `request.py`
  定义 `RoutePlanRequest`，描述起终点、场景、车辆类型、目标、约束和额外上下文。
- `dag_model.py`
  定义 `PlanningDAG`、`EnvironmentSnapshot`、`ExecutionPlan`、`ValidationReport` 和最终 `RoutePlanResponse`。

这些模型承担两类职责：

1. 给 FastAPI 提供清晰的接口文档。
2. 把 LLM 和服务层之间的数据格式固定下来，避免“能跑但结构飘”的问题。
