# 02 — System Architecture

## 1. 总览

第一版采用模块化流水线：

```text
Microphone
   │
   ▼
Audio Capture
   │
   ▼
VAD
   │
   ▼
Turn Detector
   │
   ▼
Streaming ASR
   │
   ▼
Conversation Core
   │
   ▼
LLM
   │
   ▼
Text Chunker
   │
   ▼
Streaming TTS
   │
   ▼
Audio Playback
```

旁边始终存在两条控制链：

```text
Interruption Manager
Latency Metrics
```

## 2. 每个模块负责什么

### Audio Capture

负责：

- 从麦克风持续读取 PCM 音频；
- 固定采样率/声道；
- 送入后续流水线；
- 不做“智能判断”。

### VAD

VAD = Voice Activity Detection。

只回答：

> 现在有没有人在说话？

第一候选：Silero VAD。

它不判断“这句话说完了吗”。

### Turn Detector

回答：

> 用户这一次表达真的结束了吗？

第一候选：Pipecat Smart Turn。

例子：

```text
“我觉得这个比赛吧……”
```

用户虽然停顿，但语义/语气仍像未完成。

Turn Detector 应继续等待，而不是立刻让派蒙回复。

### Streaming ASR

负责：

> 音频 → 实时文字。

第一阶段允许使用云端 Streaming ASR，优先低延迟和中文稳定性。

必须做 Provider 抽象，后续可接：

- FunASR / SenseVoice；
- Whisper 类后端；
- 商业流式 ASR。

### Conversation Core

整个项目的核心。

负责：

- 当前是谁在说话；
- 现在处于什么状态；
- 用户是否真的说完；
- 派蒙当前能不能说；
- 派蒙是否刚刚被打断；
- 实际播放到哪句话；
- 是否应该主动开口；
- 用户是否要求暂时安静。

Conversation Core 不负责生成语言。

### LLM

负责：

> “如果现在轮到派蒙说，它应该说什么？”

LLM 不负责轮次检测。

LLM 不应该决定底层音频状态。

第一版使用 OpenAI-compatible adapter，方便赛马多个模型。

最重要指标：TTFT（Time To First Token）。

### Text Chunker

LLM 不应生成完整一段后再交给 TTS。

推荐：

```text
LLM stream:
“哈？”
   ↓ 立即送 TTS

“你居然真的这么想？”
   ↓ 继续送 TTS
```

Text Chunker 按自然语义边界切分，减少首音频等待时间。

### Streaming TTS

负责：

> 文字 → 声音。

关键指标：

- TTFA / Time To First Audio；
- 中文自然度；
- 情绪表现；
- 是否支持 streaming；
- 是否容易立即取消。

### Audio Playback

必须支持：

- 流式播放；
- 立即 stop；
- 清空尚未播放 buffer；
- 能知道“实际播到了哪里”。

最后一点用于处理“被打断后的真实上下文”。

## 3. 状态机

推荐最小状态：

```text
IDLE
LISTENING
POSSIBLE_END
THINKING
SPEAKING
INTERRUPTED
SILENCED
```

典型路径：

```text
IDLE
 ↓
LISTENING
 ↓
POSSIBLE_END
 ↓ Smart Turn = complete
THINKING
 ↓
SPEAKING
 ↓ 用户开始讲话
INTERRUPTED
 ↓
LISTENING
```

`SILENCED` 用于：

> “派蒙你先闭嘴。”

在一个可配置窗口内禁止主动发言。

## 4. 不把所有东西塞进 LLM

错误：

```text
Audio → LLM → 一切
```

正确：

```text
VAD           → 专业模块
Turn Detection→ 专业模块
ASR           → 专业模块
LLM           → 语言与策略
TTS           → 专业模块
```

Agent 只处理适合 Agent 的问题。

## 5. Provider 边界

建议统一接口：

```python
class ASRProvider:
    async def stream(...): ...

class LLMProvider:
    async def stream_reply(...): ...

class TTSProvider:
    async def stream_audio(...): ...
    async def cancel(...): ...
```

这样后续所有模型都可替换，不污染 Conversation Core。
