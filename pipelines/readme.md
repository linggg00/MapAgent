# 工作流层 (Pipelines Layer)

### 职责
实现 **Agentic Workflow（智能体工作流）**。它是串联 LLM、高德 API 和物理校验引擎的“粘合剂”。

### 核心逻辑 (agent_core.py)
1. **感知**: 从 `amap_client` 抓取环境信息。
2. **规划**: 将环境信息喂给 LLM 生成初版 DAG。
3. **验证**: 将 DAG 送入 `validator.py`。
4. **迭代 (Reflection)**: 如果校验失败，将报错信息反馈给 LLM 重写，直到生成合法的物理路径。
5. **增强**: 最终合法的 DAG 送入 `amap_client` 换取导航链接，并送入 `narrator` 生成解说。

### 特性
- 具备“自我修正”能力。
- 全流程日志透明化，可记录每一轮的思考和纠错过程。