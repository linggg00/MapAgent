# 接口层 (API Layer)

当前提供两个接口：

- `GET /api/route/health`
  用于健康检查。
- `POST /api/route/plan`
  输入 `RoutePlanRequest`，返回完整 `RoutePlanResponse`。
- `POST /api/route/feedback`
  输入 `StrategyFeedbackRequest`，把用户反馈写回策略记忆。

这一层只做三件事：

1. 接收前端参数。
2. 调用 `PDRAgentCore`。
3. 把结构化结果返回给调用方。

复杂逻辑全部留在 `pipelines` 和 `services`，这样接口层保持很薄，便于后续加鉴权、异步队列或任务缓存。
