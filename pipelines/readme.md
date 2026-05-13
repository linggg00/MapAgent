# 工作流层 (Pipelines Layer)

`agent_core.py` 负责落地 PDR 闭环：

1. `AmapClient` 先构造环境快照。
2. `LLMClient` 生成第一版规划 DAG。
3. `AmapClient` 枚举多个候选执行策略。
4. `Validator` 对每个候选策略生成的执行计划进行约束校验。
5. `RewardPolicyService` 按奖励函数对候选策略打分，并执行信用分配与策略更新。
6. 如果最优候选仍失败，则把错误摘要反馈给 `LLMClient`，触发反思重试。
7. 校验通过后，`LLMClient` 生成最终解说。

这个层的目标是把“模型能力”“确定性规则”和“轻量学习闭环”串成一条可观测的业务流水线。
