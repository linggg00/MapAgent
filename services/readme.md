# 核心服务层 (Services Layer)

### 职责
封装所有外部能力和硬核逻辑。这里是“透明化”的核心，所有“非模型”的确定性计算都在这里执行。

### 核心模块
- `amap_client.py`: 
  - 调用高德 Web API 获取实时天气、路况。
  - 调用路径规划 API 进行节点拟合。
  - 生成 `amap://` 协议的 URI 导航链接。
- `validator.py`: **物理引擎**。纯代码实现的逻辑层，校验 LLM 生成的路线在物理上是否可行（如：平均车速是否超过暴雪限速）。
- `llm_client.py`: 封装与 AutoDL 云端 Llama-3 (vLLM/OpenAI API) 的通信逻辑。