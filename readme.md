# MapAgent

一个按 `Planning -> Decision -> Reflection` 闭环组织，并补入轻量强化学习机制的地图智能体原型。它面向“路线规划、极端场景出行规划、约束冲突判断”这类任务，既能接真实高德和 LLM，也能在没有 key 的情况下用 mock 数据完整跑通链路。

## 目录职责

- `api/route_api.py`：对外 REST 接口，接收请求并返回最终规划结果。
- `pipelines/agent_core.py`：PDR 中枢，串联规划、决策、校验和反思重试。
- `schemas/`：Pydantic 数据契约，约束输入、DAG、环境快照、执行计划和响应结构。
- `services/amap_client.py`：封装高德数据访问与 mock 环境构造，同时给出执行计划。
- `services/llm_client.py`：封装在线 LLM 调用，以及离线规则回退。
- `services/validator.py`：执行 DAG 结构、时效、限速、限高、限重、续航等校验。
- `services/reward_policy.py`：实现奖励函数、信用分配和策略价值更新。
- `templates/`：规划代理与解说代理的 prompt 模板。

## 运行方式

```bash
python main.py
```

服务默认启动在 `http://127.0.0.1:6006`，Swagger 文档地址为 `http://127.0.0.1:6006/docs`。

## 强化学习层

当前实现补入了和申请书一致的三块机制：

- 奖励函数：对可行性、安全性、时效、效率、舒适度、补能、合规性和反思代价做加权评分。
- 信用分配：把奖励和惩罚回溯到 DAG 节点与 `planning/decision/reflection` 三个阶段。
- 在线策略更新：维护按场景切分的策略价值表，用多臂老虎机式的 `value estimate + exploration bonus` 选择候选策略，并把本轮回报写回策略记忆。

策略记忆默认保存在 `runtime/policy_store.json`。如果调用 `POST /api/route/feedback`，用户反馈也会继续更新策略价值。

## 最小请求示例

```json
{
  "start": "北京顺义区首都机场T3航站楼",
  "destination": "张家口崇礼云顶滑雪场",
  "scene": "暴雪天气，部分高速封闭",
  "user_request": "30分钟内赶到目的地，生成安全路线规划任务 DAG",
  "vehicle_type": "sedan",
  "arrival_deadline_minutes": 30,
  "hard_constraints": ["避开封闭高速", "不能超过当前天气安全限速"],
  "soft_constraints": ["优先时效"],
  "use_mock_data": true
}
```

## 环境变量

- `AMAP_API_KEY`：可选，提供后启用高德 geocode/weather。
- `MAP_AGENT_LLM_API_KEY`：可选，OpenAI 兼容接口 key；如果本地服务不鉴权，可以不填。
- `MAP_AGENT_LLM_BASE_URL`：可选，本地或远端 OpenAI 兼容接口地址。
- `MAP_AGENT_LLM_MODEL`：可选，填你们本地微调模型暴露出来的模型名。

## 当前实现特点

- 离线可跑：没有外部 key 也能返回环境快照、任务 DAG、执行计划和反思轨迹。
- 强约束优先：对续航不足、超速、限高、限重、危化品合规等问题会明确报错。
- 轻量 RL 已接入：系统会在多种候选执行策略之间打分、选优、归因并更新价值估计。
- 可扩展：后续可以把 `services/amap_client.py` 中的 mock 逻辑替换成真实导航、路径拟合和实时路况，把当前价值表升级为真正的策略网络。
