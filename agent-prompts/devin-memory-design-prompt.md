# Devin 任务：Paimon 记忆系统设计评估（只读，不写代码）

## 工作目录
`/home/haa/sites/paimon-voice-poc`

## 角色
你是架构设计师，**只读调研，不修改任何代码**。

## 背景
用户批准使用 Memsearch（Zilliz 开源，MIT 协议）作为记忆系统备选。但当前阶段**不需要真正实现**，只需要：
1. 产出完整的设计方案
2. 评估对现有 Task 路线图的影响
3. 给出 Demo 时的"假记忆"方案（看起来像有记忆，实际是 mock）
4. 实际接入留到后续阶段

## 你的任务
产出 `.loom/design/MEMORY_SYSTEM_DESIGN.md`，包含：

### 1. Memsearch 能力与限制
- Memsearch 是什么、能做什么、不能做什么
- 与 Milvus 的关系（是否必须用 Milvus？能否用本地文件后端？）
- 安装/运行成本（内存、依赖、部署方式）
- API 接口概览（add / search / consolidate / forget）
- 与现有 Provider 抽象层的关系（是否要新增 MemoryProvider？还是直接嵌进 LLMProvider？）

### 2. 记忆系统设计
- **记忆分层**：短期（对话内）/ 中期（会话间）/ 长期（跨会话）怎么分
- **记忆类型**：事实型（用户偏好、项目背景）/ 经验型（上次对话结论、失败教训）/ 人格型（派蒙的行为模式、情绪状态）
- **写入时机**：什么时候把信息存入记忆？（对话结束？用户显式说"记住"？AI 判断重要时自动写？）
- **检索时机**：什么时候召回记忆？（对话开始？话题相关时？人格判断需要上下文时？）
- **遗忘机制**：记忆什么时候过期？怎么清理？（TBD 也可以，但要写出来）

### 3. 与现有架构的关系
- 记忆系统属于哪个模块？（Provider？Conversation Core？Paimon 人格？）
- 是否影响现有 TASK-009（派蒙人格）/ TASK-010（pipeline 组装）/ TASK-011（InitiativePolicy）？
- 如果现在不改代码，这些 Task 是否可以先不做记忆相关部分，留到后续？
- 如果要改，建议怎么调整 Task 路线图？

### 4. Demo 方案（假记忆）
- Demo 时不需要真实记忆系统，但要让观众"看起来像有记忆"
- 方案 A：硬编码几条"记忆"（用户上次说喜欢XX、派蒙记得上次对话结论），在特定触发时机显示
- 方案 B：用一个本地 JSON 文件模拟记忆库，read/write 都是文件操作，不依赖向量
- 方案 C：在 LLM prompt 里注入"记忆上下文"（system prompt 带一段伪记忆），最简单
- 推荐哪个方案？为什么？
- 给出 Demo 时的具体话术/交互设计（例如：派蒙说"我记得你上周说过..."）

### 5. 实际接入路线图（未来）
- 如果后续真要接 Memsearch，需要改哪些文件？
- 新增哪些接口？
- 数据迁移策略（从 mock 迁移到真实记忆）
- 预估工程量（以 Devin 单任务计）

### 6. 决策建议
- 现在：不做真实记忆，只做 Demo 假记忆
- 后续：什么时候该接真实记忆系统？
- 触发条件是什么？（用户量？数据量？演示需求变化？）

## 约束
- **不修改任何代码**
- **不修改 00_READ_ME_FIRST.md 到 07_DECISIONS_AND_OPEN_QUESTIONS.md**
- **不修改 .loom/tasks.json**（只读评估，不改 Task 状态）
- 只产出 `.loom/design/MEMORY_SYSTEM_DESIGN.md`

## 你需要读的
- `.loom/design/system.md`
- `.loom/design/REALTIME_PLUS_EVALUATION.md`
- `.loom/tasks.json`（TASK-009 ~ TASK-012）
- `.loom/capabilities/` 下的人格/对话相关 capability
- `src/providers/` 目录结构
- `AGENTS.md`
