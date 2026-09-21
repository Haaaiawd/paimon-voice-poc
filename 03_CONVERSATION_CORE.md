# 03 — Conversation Core

> 这是本项目最重要的文档。

## 1. Conversation Core 是什么

如果把模型供应商全部删掉，本项目仍然应该保留下来的部分，就是 Conversation Core。

它解决：

- 谁现在应该讲话；
- 用户是不是讲完了；
- 派蒙能不能抢一句；
- 用户打断派蒙后发生什么；
- 派蒙上一句话到底被用户听到了多少；
- 是否该主动说；
- 什么时候必须闭嘴。

## 2. 四个核心组件

### 2.1 TurnManager

输入：

- VAD 状态；
- Smart Turn 状态；
- ASR partial/final；
- 当前 Conversation State。

输出：

- `USER_TURN_STARTED`
- `USER_TURN_CONTINUES`
- `USER_TURN_COMPLETE`
- `AGENT_CAN_RESPOND`

原则：

**停顿不是完成。**

### 2.2 InterruptionManager

当派蒙正在说话，用户重新开口：

```text
SPEAKING
  ↓
VAD speech_started
  ↓
立刻触发 interruption
```

至少同时执行：

1. 停止音频播放；
2. 清空未播放 TTS buffer；
3. 尝试取消仍在生成的 LLM；
4. 记录已播放内容；
5. 状态切为 `INTERRUPTED`；
6. 重新进入 `LISTENING`。

目标：

> 用户一开口，派蒙尽可能快地闭嘴。

### 2.3 InitiativePolicy

回答：

> 用户没问我时，我现在该主动说吗？

第一版不要用复杂模型。

可以用可解释的评分：

```text
initiative_score =
    silence_duration
  + interesting_context
  + direct_mention
  + conversation_energy
  - recently_spoke
  - user_is_speaking
  - user_requested_silence
```

硬规则优先级最高：

```text
if user_is_speaking:
    NEVER_SPEAK

if silenced:
    NEVER_SPEAK
```

达到阈值后，不是“必须说”。

而是允许调用一次：

```text
should_say_something(context)
```

LLM 仍可返回 `NOOP`。

一个好 Companion 必须有闭嘴能力。

### 2.4 ContextManager

维护两种历史：

#### Logical History

模型生成过什么。

#### Heard History

用户实际上听到了什么。

这两个不能混。

## 3. 被打断后的上下文

例子：

派蒙生成：

> “我觉得你今天这个发型特别像一只刚睡醒的史莱姆。”

但实际播放：

> “我觉得你今天这个发型特别像——”

用户：

> “你敢说完试试。”

下一轮 LLM 应看到：

```text
assistant_heard:
“我觉得你今天这个发型特别像——”

assistant_generated_but_not_heard:
“一只刚睡醒的史莱姆。”

user:
“你敢说完试试。”

event:
assistant_was_interrupted
```

不能直接把完整生成句子写入普通聊天历史。

否则模型会认为用户听到了事实上没有听到的内容。

## 4. Conversation Events

建议内部一切都事件化：

```text
MIC_AUDIO
USER_SPEECH_STARTED
USER_SPEECH_STOPPED
TURN_COMPLETE
ASR_PARTIAL
ASR_FINAL
LLM_STARTED
LLM_TOKEN
TTS_STARTED
FIRST_AUDIO
AGENT_SPEAKING
AGENT_INTERRUPTED
PLAYBACK_STOPPED
INITIATIVE_TRIGGERED
SILENCE_REQUESTED
```

未来接直播、视觉、动作时，可以继续加：

```text
HOST_WAVE
HOST_FALL
CHAT_MESSAGE
DONATION_EVENT
```

而不用推翻核心架构。

## 5. Agent 输入结构

不要只给 LLM 一串聊天文本。

推荐构造：

```json
{
  "character": "paimon",
  "state": "THINKING",
  "last_user_text": "...",
  "recent_heard_history": [],
  "interruption_context": null,
  "silence_duration_ms": 0,
  "initiative_reason": null,
  "behavior_constraints": {}
}
```

## 6. Agent 输出结构

第一版即使没有 3D，也建议输出结构化结果：

```json
{
  "speech": "哈？你认真的？",
  "emotion": "teasing",
  "energy": 0.8,
  "should_continue": false
}
```

以后 `emotion` 可以直接驱动：

- TTS 情绪；
- 表情；
- 3D 动作。

这样现在的语音核心能自然扩展到后面的虚拟角色。

## 7. 最重要的设计原则

### 快速，但不要抢话

不要为了低延迟牺牲轮次自然度。

### 短句优先

实时语音中：

> “哈？真的假的？”

通常比 80 字解释更自然。

### 可被打断

任何 TTS/LLM 设计都必须允许取消。

### 用户实际听到什么，历史就记什么

这是语音 Agent 和普通 Chatbot 最大的不同之一。
