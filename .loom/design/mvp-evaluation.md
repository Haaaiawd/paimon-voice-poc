# MVP 验收与评估方法

- Kind: verification
- Status: confirmed
- 规范文档：`06_MVP_AND_EVALUATION.md`

## Responsibility in the whole

定义"什么叫做完"：最小闭环、延迟指标、测试用例、赛马方法与主观评分。

## Inputs, outputs, and boundaries

最小闭环：Mic → VAD → Turn → ASR → Core → LLM stream → TTS stream → Playback + Barge-in。
第一版 UI = Terminal（状态 + 对话流 + 延迟数字，格式见 `06` §2）。

## Components and control flow

逐轮记录时间戳：t_user_speech_end / t_turn_confirmed / t_asr_final / t_llm_first_token /
t_tts_request / t_first_audio / t_playback_start / t_interrupt_detected / t_playback_stopped。

核心指标：
- SEFA（Speech End → First Audio）目标区间 500–800ms，非硬承诺，benchmark 后修正；
- Barge-in Stop Latency 越接近即时越好。

## Data and state

latency log 写 `data/`（gitignore）；主观评分 1–5 分七维度（`06` §5）。

## Interfaces and dependencies

LLM 赛马记录：TTFT / 结构化输出成功率 / 人格稳定 / 短回复 / 中文口语 / 成本。
TTS 赛马并入 TASK-008（Fish vs 百炼）：TTFA / 中文自然度 / 情绪 / 长短句 / 取消速度 / 稳定性。

## Failure, safety, and recovery

"终端版本不好聊，加 GUI 没意义"——终端体验是验收前置条件。

## Implementation constraints

MVP 完成定义（`06` §8）：连续 ≥10 分钟、句中停顿不抢话、可打断且历史正确、延迟可接受、
人格明显、≥2 LLM + ≥2 TTS 可替换（注：TTS 第一轮 Fish + 百炼双 adapter 赛马，见 D-014）、
有基础 latency log。

## Verification strategy

测试用例 A–G（`06` §4）：一问一答 / 句中停顿 / 半句打断 / 要求安静 / 长沉默 /
连续讲话 / 连续互怼。由 TASK-012 执行并产出 eval 记录。

## Related documents and capabilities

`01_PRODUCT_SCOPE.md` §5（成功标准）、TASK-003（赛马数据先于本验收）。
