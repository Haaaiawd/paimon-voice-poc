# Conversation Core：轮次/打断/主动性/上下文

- Kind: system
- Status: confirmed
- 规范文档：`03_CONVERSATION_CORE.md`（本项目最重要的文档）

## Responsibility in the whole

项目的核心资产。删掉所有模型供应商后仍应保留的部分。模型可换，这层必须自研。

## Inputs, outputs, and boundaries

- TurnManager：VAD + Smart Turn + ASR partial/final + 当前状态 → USER_TURN_STARTED /
  CONTINUES / COMPLETE / AGENT_CAN_RESPOND。原则：停顿不是完成。
- InterruptionManager：用户开口 → 停播、清 TTS buffer、取消 LLM、记录已播内容、
  INTERRUPTED → LISTENING。
- InitiativePolicy：可解释评分（silence_duration + interesting_context + direct_mention
  + conversation_energy − recently_spoke − user_is_speaking − user_requested_silence）。
  硬规则：user_is_speaking 或 silenced → NEVER_SPEAK。达阈值只获得"问一次"资格，
  LLM 可答 NOOP。
- ContextManager：Logical History 与 Heard History 分离。

## Components and control flow

内部一切事件化（doc 03 §4）：MIC_AUDIO、USER_SPEECH_STARTED/STOPPED、TURN_COMPLETE、
ASR_PARTIAL/FINAL、LLM_STARTED/TOKEN、TTS_STARTED、FIRST_AUDIO、AGENT_SPEAKING、
AGENT_INTERRUPTED、PLAYBACK_STOPPED、INITIATIVE_TRIGGERED、SILENCE_REQUESTED。

## Data and state

被打断后的上下文结构（doc 03 §3）：assistant_heard / assistant_generated_but_not_heard /
user / event=assistant_was_interrupted。不能把未播出的生成内容写进普通历史。

## Interfaces and dependencies

Agent 输入（doc 03 §5）与输出（§6：speech/emotion/energy/should_continue）为结构化 JSON，
为后续 TTS 情绪、表情、3D 动作预留。

## Failure, safety, and recovery

- 不为低延迟牺牲轮次自然度。
- 短句优先（1–2 句默认）。
- 任何 TTS/LLM 环节必须可取消。
- 用户实际听到什么，历史就记什么。

## Implementation constraints

第一版不真正打断正在讲话的用户（D-008）；主动插话仅限轮次结束/长沉默/明显留白。

## Verification strategy

TASK-005（状态机+TurnManager 单测）、TASK-006（打断六步 + heard history 断言）、
TASK-011（InitiativePolicy + SILENCED）。

## Related documents and capabilities

`02_SYSTEM_ARCHITECTURE.md`（状态机）、`05_PAIMON_PERSONA.md`（行为表现）。
