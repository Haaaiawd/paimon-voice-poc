# Project Whole and Document Map

> This is the concise entry point, not the container for every design decision. Describe the whole and
> link the documents that make it buildable. Add or remove documents according to project complexity.
> The Agent uses `loom context` to compile this with the active Task and referenced files.

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

## Design document map

| 文档 | 决策面 |
|------|--------|
| `00_READ_ME_FIRST.md` | 接手入口、已拍板决定、核心资产清单 |
| `01_PRODUCT_SCOPE.md` | 产品目标、核心体验十条、问题优先级 |
| `02_SYSTEM_ARCHITECTURE.md` | 模块流水线、状态机、Provider 边界 |
| `03_CONVERSATION_CORE.md` | 四大核心组件、事件模型、heard history |
| `04_TECH_STACK_AND_OPEN_SOURCE.md` | 选型：Pipecat/VAD/ASR/TTS/LLM 候选 |
| `05_PAIMON_PERSONA.md` | 人格、拌嘴机制、情绪标签、prompt 原则 |
| `06_MVP_AND_EVALUATION.md` | MVP 闭环、延迟指标、测试用例 A–G、赛马方法 |
| `07_DECISIONS_AND_OPEN_QUESTIONS.md` | 已决定/未决定清单、开发前待确认项 |

## Professional capability map

暂无。候选领域（需要时再 `loom capability add`）：实时语音轮次检测、barge-in/打断工程、
低延迟 pipeline 调优、中文 TTS 选型评估。

## Project structure

Point to `.loom/STRUCTURE.md` — where source code, tests, docs, assets, and configuration files live.
The Agent reads this before creating or moving files.

## Work map

Point to `.loom/tasks.json`; do not duplicate volatile Task state here. Each Task uses
`acceptance[]` with `criterion`, `verify_by`, and `evidence` fields. Completion requires one
`acceptance_results` entry per criterion with concrete evidence. Use `done_when[]` only for legacy
Tasks.

## Decision history

Consequential changes to existing decisions go in `.loom/DECISIONS.md`. Use `loom decision --json-file`
to record what changed, why, and which tasks were affected. `loom check` warns when a done Task is
marked affected by a later decision.

## Completion and failure

完成 = `06_MVP_AND_EVALUATION.md` §8 全部满足：连续运行 ≥10 分钟、句中停顿不抢话、
可打断且历史正确、延迟达标、人格明显、≥2 LLM + ≥2 TTS 可替换、有 latency log。

看似完成实则失败：延迟达标但轮次感像客服；能打断但打断后上下文错乱；只在 demo 句子
上表现好，自由聊天崩坏。

## Staged visibility and review

The human funds this project with attention and patience. Long stretches without visible progress
erode that patience, even when the work is sound. Design the Work Map so the human sees the project
growing, not just LOOM state changing.

- **Human-visible acceptance**: when designing Tasks, prefer acceptance criteria whose evidence is
  something the human can see or feel — a command running, a page rendering, a file with real content,
  a test passing in front of them. Machine-only verification is valid but should not be the only thing
  the human sees for long stretches.
- **Staged showcase**: every few Tasks, or at each natural project milestone, show the human something
  real that now works. Run the CLI, open the page, display the data, walk through the flow. A working
  thing creates momentum; a status update does not.
- **Staged review**: at material checkpoints, review what was built — run tests, inspect code quality,
  check against design intent. Catch drift early while it is cheap to fix. Tell the human what passed
  and what surprised you.
- **Verification gate**: after a batch of Tasks, run `loom check` and the project's own tests together.
  Both should pass before telling the human the batch is done. If tests fail or coverage drops, fix
  before moving on — do not let partial work accumulate behind a green-looking summary.
- **Excitement is a feature**: if the project has a surface the human will enjoy seeing — a UI, a CLI
  with clean output, a visualization, a working demo — prioritize reaching that surface early. The
  human's "I want to see more of this" feeling is real project fuel. Do not save the satisfying part
  for last if an early slice can deliver it.

## Keeper handoff

Before material execution, run `loom project ready` to freeze a digest, then ask a fresh Agent to
run `loom keeper prompt` and `loom keeper record`. Repair findings and prepare again;
a fresh Keeper must verify closure, including minor gaps. See `loom review --help`.
