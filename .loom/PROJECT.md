# Paimon Voice PoC — Project Map

## Intended result

一个运行在 PC 本地的实时语音 Companion（角色：派蒙）。成功 = 用户能连续自然聊 5–10 分钟：
轮次判断准确、可被打断且上下文不错乱、偶尔主动插话、人格稳定、回复口语化且短。
核心量化目标：SEFA（用户讲完→派蒙出声）进入 500–800ms 区间。

## People and operating reality

内部技术验证原型。使用者是开发者本人；协作方是接手开发的 AI Agent。
不是对外产品，不涉及 IP 产品化；派蒙角色仅用于内部验证"人格化实时互动"这一命题。

## Whole experience or behavior

```text
麦克风 → VAD → Turn Detection → Streaming ASR → Conversation Core
     → LLM (streaming) → Text Chunker → Streaming TTS → 扬声器
```

用户可随时插嘴，派蒙立即停止播放并知道自己被打断；安静时按规则偶尔主动开口；
用户要求安静时进入 SILENCED。Terminal UI，无 GUI。

## Boundaries and consequential assumptions

- 第一阶段不做：3D、X4 Air、直播、手机 App、视觉、长期记忆、自训练模型。
- Conversation Core（轮次/打断/主动性/上下文/状态机）必须自研，是核心资产。
- ASR/LLM/TTS 一律走 Provider 抽象，业务层不直接 import 供应商 SDK。
- 不要把任务扩成"通用语音 Agent 平台"。
- 第一版默认不真正打断正在讲话的用户；开发用耳机规避 AEC。
- 开发环境：Windows 原生 Python 3.12（音频闭环不走 WSL）；Docker 仅用于依赖打包。
- API keys 入 `.env`，不进仓库；DashScope key 申请中（TASK-007 的前置）。

## Design document map

| 设计文档 | 规范来源 | 决策面 |
|----------|----------|--------|
| `.loom/design/product-scope.md` | `01_PRODUCT_SCOPE.md` | 范围、体验十条、问题优先级 |
| `.loom/design/system-architecture.md` | `02_SYSTEM_ARCHITECTURE.md` | 流水线、状态机、Provider 边界 |
| `.loom/design/conversation-core.md` | `03_CONVERSATION_CORE.md` | 四大组件、事件模型、heard history |
| `.loom/design/tech-stack.md` | `04_TECH_STACK_AND_OPEN_SOURCE.md` | 选型与赛马方法 |
| `.loom/design/paimon-persona.md` | `05_PAIMON_PERSONA.md` | 人格、情绪标签、prompt 原则 |
| `.loom/design/mvp-evaluation.md` | `06_MVP_AND_EVALUATION.md` | MVP 闭环、指标、用例 A–G |

根目录 `00_`–`07_*.md` 是规范文档本体（编号即阅读顺序）；`.loom/design/` 是决策面索引。
`07` 的已决定项已进入 `DECISIONS.md`（D-001–D-011）。

## Professional capability map

| 能力域 | 决策树 | 影响的设计决策 |
|--------|--------|----------------|
| `.loom/capabilities/turn-taking/` | C1–C6：endpointing 策略、VAD 参数、兜底、barge-in、双历史、投机 | Smart Turn 接线、InterruptionManager、ContextManager |
| `.loom/capabilities/low-latency-pipeline/` | C1–C5：预算分配、重叠执行、慢轮次 cue、buffer、指标纪律 | 延迟架构、ack cue、latency log |
| `.loom/capabilities/chinese-tts-eval/` | C1–C5：WS 协议、flush 时机、cancel 实测、测句集、情绪映射 | TTSProvider 接口、Text Chunker、赛马方法 |
| `.loom/capabilities/streaming-asr-zh/` | C1–C4：断句模式、静音阈值、断句权责、语气词 | ASR adapter 事件面、Paraformer 参数 |

四个 dossier 均为 agent provisional 确认；关键分歧点（如 backchannel 容忍、投机深度）
等真实数据再升级 human 确认。

## Project structure

见 `.loom/STRUCTURE.md`：`src/conversation/`（核心）、`src/providers/asr|llm|tts/`、
`src/turn/`、`src/character/`、`src/metrics/`、`src/runtime/`、`tests/`、`scripts/`。
业务层只经 adapter 依赖供应商。

## Work map

`.loom/tasks.json`：12 个 task 覆盖全部 11 个 deliverable。执行序：
TASK-001（环境）→ 002（接口）/004（音频轮次）/005（Core）并行 → 003（赛马）、
006（打断）、007（ASR，blocked on key）、008（TTS）、009（人格）→ 010（端到端+UI+metrics）
→ 011（主动性）→ 012（MVP 验收）。

## Decision history

`.loom/DECISIONS.md`。变更用 `loom decision --json-file` 记录， affected done task 会被
`loom check` 标记需重开。

## Completion and failure

完成 = `06_MVP_AND_EVALUATION.md` §8 逐条满足。看似完成实则失败：延迟达标但轮次感像
客服；能打断但历史错乱；只在 demo 句上好用、自由聊天崩坏；capability 决策树被绕过
（如 ASR 断句直驱轮次）而无人察觉。

## Staged visibility and review

工作图按"尽早有可看的东西"排：TASK-004 结束就能对麦克风看到 VAD/Smart Turn 实时判定；
TASK-010 结束就能实际对话。每批 task 完成后跑 `loom check` + 项目自身测试再汇报。
阶段性向人展示可运行物，不只报状态。

## Keeper handoff

执行前：`loom project ready` 冻结 digest，由新 Agent 跑 `loom keeper prompt` +
`loom keeper record` 做独立性校验；修复发现项后重新 ready。
