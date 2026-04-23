# 🧠 智能体中枢：规划-决策-反思 (PDR) 架构详解

## 1. 架构概述 (Overview)

本系统的核心工作流（位于 `pipelines/agent_core.py`）摒弃了传统大模型“端到端”（直接生成结果）的黑箱模式，转而采用了基于 **Neuro-Symbolic AI（神经符号人工智能）** 的 **PDR (Planning-Decision-Reflection) 三层代理架构**。

该架构将复杂的地图导航任务解耦为三个独立的智能环节，通过引入硬编码的物理校验器（Validator），实现了大模型语义理解能力与传统软件确定性计算的完美结合，从根本上杜绝了 AI 幻觉引发的物理安全风险。

---

## 2. 核心角色解析 (The Three Agents)

### 🗺️ 1. 规划代理 (Planning Agent)
- **核心职责**：负责“宏观战术制定”。它接收用户的自然语言指令（包含隐含的极端约束，如暴雪、危化品），利用大语言模型（LLM）的思维链（CoT）能力，将复杂意图拆解为包含地理节点和预期连线关系的初版任务 DAG（有向无环图）。
- **技术实现**：依靠 `templates/planner_cot.txt` 驱动 LLM，强迫模型在输出 DAG 前先进行纸面上的逻辑推演。

### ⚙️ 2. 决策代理 (Decision Agent)
- **核心职责**：负责“工具调用与微观执行”。它是系统的“手和眼”，负责将规划代理生成的虚拟节点与真实的物理世界对接。
- **技术实现**：
  - 调用 `services/amap_client.py`，获取高德地图的实时天气、路网封闭状况及经纬度信息。
  - 调用 `services/validator.py`，执行硬核物理计算（如：`路段耗时 = 距离 / 速度`，并验证所需速度是否超过当前天气的物理安全极限）。

### 🔍 3. 反思代理 (Reflection Agent)
- **核心职责**：负责“自我纠错与闭环重构”。当决策代理在物理校验中发现致命错误（如规划路线途径了限重老桥，或要求暴雪天达到 400km/h）时，反思代理会被激活。
- **技术实现**：它会捕获 `validator.py` 抛出的具体报错信息（Error Context），将其打包成“反思提示词”，重新喂给规划代理（LLM），强制模型基于报错原因修改原有 DAG，直至生成完全符合现实物理规律的合法路线。

---

## 3. 核心流程架构图 (Workflow Architecture)

以下为系统处理“复杂极端出行需求”时的完整流转生命周期：

```mermaid
graph TD
    %% 定义样式
    classDef user fill:#e1f5fe,stroke:#01579b,stroke-width:2px;
    classDef llm fill:#f3e5f5,stroke:#4a148c,stroke-width:2px;
    classDef tool fill:#e8f5e9,stroke:#1b5e20,stroke-width:2px;
    classDef logic fill:#fff3e0,stroke:#e65100,stroke-width:2px;

    User([👤 用户发起复杂指令<br>如：暴雪天重卡限时救援]):::user --> PA

    subgraph 🧠 PDR 智能体闭环中枢 (agent_core.py)
        PA[🗺️ 规划代理 Planning<br>LLM解析语义 & 生成初始DAG]:::llm
        
        PA -->|交付路线图| DA[⚙️ 决策代理 Decision<br>注入实时高德数据]:::tool
        
        DA -->|送审| VAL{📐 物理校验器 Validator<br>限速/限重/路况测算}:::logic
        
        VAL -->|❌ 校验失败: 违背物理常识| RA[🔍 反思代理 Reflection<br>提取报错原因]:::llm
        RA -.->|携带 Error Prompt 强制重构| PA
    end

    VAL -->|✅ 校验通过| EX[🚀 执行器 Executor<br>高德路径拟合 & URI生成]:::tool
    EX --> NA[🎙️ 解说代理 Narrator<br>生成路段安全提示]:::llm
    NA --> Output([🏁 返回前端: 导航链接 + 可视化DAG + 语音解说]):::user
