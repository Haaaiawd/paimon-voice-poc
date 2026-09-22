# 06 — MVP & Evaluation

## 1. 第一版最小闭环

必须完成：

```text
Microphone
 ↓
VAD
 ↓
Turn Detection
 ↓
Streaming ASR
 ↓
Conversation Core
 ↓
LLM Streaming
 ↓
Streaming TTS
 ↓
Playback
```

并支持：

```text
Barge-in
```

## 2. 第一版 UI

（更新：2026-09-22 起另有 `frontend/` 浏览器 demo，WS 契约见
`.loom/design/FRONTEND_DEMO_DESIGN.md` §4；终端入口 `runtime/main.py` 仍在。）

Terminal 足够：

```text
[LISTENING]

YOU:
我感觉这个项目吧……

Turn: INCOMPLETE

YOU:
……好像越来越有意思了。

Turn: COMPLETE

ASR final: +210ms
LLM first token: +355ms
TTS first audio: +510ms

PAIMON:
哈？你现在才发现？

[SPEAKING]

YOU:
你闭嘴。

[INTERRUPTED +93ms]

PAIMON:
……行。
```

如果终端版本不好聊，加 GUI 没意义。

## 3. 延迟指标

必须记录每一轮：

```text
t_user_speech_end
t_turn_confirmed
t_asr_final
t_llm_first_token
t_tts_request
t_first_audio
t_playback_start
t_interrupt_detected
t_playback_stopped
```

核心指标：

### SEFA

Speech End → First Audio

也就是：

> 用户讲完，到派蒙真的开始出声。

当前工作目标：

```text
500–800ms 级
```

这是目标区间，不是硬承诺。

第一轮 benchmark 后再修正。

### Barge-in Stop Latency

用户重新开口 → 派蒙真正停止出声。

目标：

越接近“即时”越好。

至少要做到明显小于普通人感受到的“抢话持续时间”。

## 4. 测试用例

### A. 普通一问一答

用户：

> “你觉得今天吃什么？”

验证：

- 正确结束；
- 快速回复。

### B. 句中停顿

用户：

> “我觉得这个东西吧……嗯……其实还行。”

验证：

- 第一次停顿不抢话。

### C. 半句打断派蒙

派蒙：

> “你这个想法真的有点——”

用户：

> “你最好想清楚再说。”

验证：

- 立刻停；
- 下一轮知道自己没说完。

### D. 用户要求安静

用户：

> “你先闭嘴两分钟。”

验证：

- 派蒙不主动插话。

### E. 长时间沉默

双方安静。

验证：

- InitiativePolicy 在合理窗口内可能主动开口；
- 不应过于频繁。

### F. 用户连续讲话

用户连续讲 30–60 秒。

验证：

- 派蒙不要一直找机会抢话。

### G. 连续互怼

测试快速短句：

> “你烦不烦？”
> “你才烦。”
> “你再说？”
> “说就说。”

验证：

- 延迟；
- 中断；
- 短上下文一致性。

## 5. 主观评分

每次 Session 后人工评：

1–5 分：

- 抢话自然度；
- 回应速度；
- 被打断反应；
- 派蒙人格稳定；
- 语言口语感；
- 声音好听程度；
- 是否愿意继续聊。

## 6. TTS 赛马

统一测试同一组句子。

记录：

```text
Provider
TTFA
中文自然度
情绪
短句表现
长句表现
取消速度
稳定性
```

不要只看官网 benchmark。

最终用真实对话体验决定。

## 7. LLM 赛马

记录：

```text
TTFT
结构化输出成功率
人格稳定
短回复能力
中文口语
成本
```

对于当前项目：

```text
TTFT > TPS
```

更重要。

## 8. MVP 完成定义

满足以下条件才算完成：

- 能连续运行至少 10 分钟；
- 句中停顿不频繁抢话；
- 用户可随时打断；
- 被打断后的历史正确；
- 对话延迟达到可接受范围；
- 派蒙人格明显；
- 可替换至少 2 个 LLM Provider；
- 可替换至少 2 个 TTS Provider（更新：第一轮 Fish + 百炼双 adapter 赛马，
  接口层面即满足"可替换"，见 D-014）；
- 有基础 latency log。
