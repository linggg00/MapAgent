# 数据校验层 (Schemas Layer)

### 职责
基于 Pydantic 维护全系统的“数据契约”。它不仅用于 FastAPI 的自动文档生成，更重要的是作为“黑箱透明化”的第一道关卡，强制要求大模型生成的 JSON 必须符合物理拓扑逻辑。

### 核心文件
- `request.py`: 定义前端请求模型（如起点坐标、车型、限时等）。
- `dag_model.py`: **关键文件**。定义 DAG 图的严格结构（nodes, edges）。如果 Llama-3 吐出的 JSON 缺少了某个字段或格式错误，此处会直接抛出 422 错误并触发重试逻辑。

### 校验项
- 节点类型 (Location/Waypoint)
- 连线关系 (From/To 必须存在于 Node ID 中)