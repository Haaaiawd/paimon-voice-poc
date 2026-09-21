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

### Pipecat 原语 vs 自研 Core 的权责边界（防双路径）

Pipecat 自带 turn strategy / InterruptionFrame / context aggregator。集成规则：

- **判定原语用 Pipecat**：VAD、Smart Turn、UserStartedSpeakingFrame 等帧事件；
- **裁决与策略归 Core**：Pipecat 事件经 adapter 翻译成我们的域事件
  （USER_SPEECH_STARTED/TURN_COMPLETE/...），状态迁移、InitiativePolicy、双历史
  只存在于 `src/conversation/`；
- **打断路径唯一**：Pipecat 内置 interruption 机制只当"媒体层信号源"用（VAD 触发
  InterruptionFrame → 我们接管停播+清 buffer+取消 LLM）；禁止 Pipecat 默认中断逻辑
  与 InterruptionManager 并行生效。TurnManager 同理：包装 Pipecat strategy 的输出，
  不重复实现判定。

### SILENCED 的生产者与退出

谁产出 SILENCE_REQUESTED、谁在 SILENCED 中识别唤醒，doc 03 未指定，此处补定：

- **进入**：Conversation Core 内的规则分类器消费 ASR_FINAL 文本，命中
  "闭嘴/别说话/安静一会儿"类指令 → 发 SILENCE_REQUESTED → 状态机进 SILENCED。
  规则分类不走 LLM（省一次往返，且确定性可测）；匹配表可配置；
- **退出**（任一）：ASR_FINAL 含"派蒙"直呼；超过 silence timeout（默认时长可配，
  或被用户指定的"两分钟"覆盖）；高优先级系统事件；
- SILENCED 中 ASR 持续转写（否则无法听到唤醒词），但不进 LLM、不触发主动插话。

## Verification strategy

TASK-005（状态机+TurnManager 单测）、TASK-006（打断六步 + heard history 断言）、
TASK-011（InitiativePolicy + SILENCED）。

## Related documents and capabilities

`02_SYSTEM_ARCHITECTURE.md`（状态机）、`05_PAIMON_PERSONA.md`（行为表现）。
