# 提示词模板层 (Templates Layer)

- `planner_cot.txt`
  用于规划代理，要求模型先覆盖环境采集、约束提取、候选搜索和安全校验，再输出 JSON DAG。
- `narrator.txt`
  用于解说代理，把最终结构化计划转成能给用户直接读的说明。

模板中保留了 `{request_context}`、`{environment_context}`、`{reflection_context}` 等占位符，方便在代码里动态注入实时上下文。
