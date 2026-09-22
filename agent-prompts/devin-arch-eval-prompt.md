# Devin 任务：Paimon 架构评估 — Qwen Realtime Plus vs 现有 Pipecat 分体路线

## 工作目录
`/home/haa/sites/paimon-voice-poc`

## 角色
你是架构评估师，**只读调研，不修改任何代码**。

## 背景
阿里百炼发布了 `qwen-audio-3.0-realtime-plus`（Qwen Realtime Plus），是一个端到端实时语音大模型，支持：
- 输入：Audio + Text
- 输出：Audio + Text
- WebSocket / AOQ / WebRTC 三种接入协议
- server_vad / smart_turn / manual 三种轮次模式
- Function Calling
- 声音复刻，55 种音色
- 延迟：全球 Artificial Analysis Speech-to-Speech 评测第一
- 价格：输入音频 40元/百万tokens，输出文本+音频 150元/百万tokens

## 当前架构
- Provider 抽象层：ASRProvider / LLMProvider / TTSProvider
- Pipecat 分体路线：ASR → LLM → TTS 串行
- TASK-007 刚完成 DashScope ASR adapter
- TASK-008 待做 Fish Audio + CosyVoice TTS adapter

## 你的任务
产出 `.loom/design/REALTIME_PLUS_EVALUATION.md`，包含：

### 1. Realtime Plus 能力清单
- 它能做什么、不能做什么
- 协议细节（WebSocket / AOQ / WebRTC 的差异）
- 延迟数据、价格、限流
- 与 Pipecat 各组件的能力映射

### 2. 架构对比
- **分体路线**：ASR → LLM → TTS 的优缺点
- **端到端路线**：Realtime Plus 直接双工的优缺点
- 延迟、成本、复杂度、可控性、可替换性

### 3. 接入可行性
- 现有 Provider 抽象层能否适配 Realtime Plus？
- 需要改哪些接口？
- 是否需要保留 Pipecat 作为 fallback？
- 混合架构是否可行？

### 4. 推荐方案
- 给出 2-3 个选项（纯分体 / 纯端到端 / 混合）
- 每个方案的工程量估算
- 明确推荐一个，并说明理由

### 5. 对 Task 路线图的影响
- 如果走推荐方案，TASK-008/TASK-009/TASK-010 需要如何调整？
- 哪些 Task 可以合并/删除/重写？

## 约束
- **不修改任何代码**
- **不修改 00_READ_ME_FIRST.md 到 07_DECISIONS_AND_OPEN_QUESTIONS.md**
- 只产出 `.loom/design/REALTIME_PLUS_EVALUATION.md`
- 基于真实文档和代码，不要编造数据

## 你需要读的
- `.loom/design/system.md`
- `.loom/capabilities/ai-review-system/capability.md`
- `.loom/tasks.json`（TASK-007 ~ TASK-012）
- `src/providers/` 目录结构
- 阿里百炼官方文档（你已有搜索结果的摘要，必要时 web_search）
