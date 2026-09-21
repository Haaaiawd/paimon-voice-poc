# 04 — Tech Stack & Open Source

## 1. 原则

我们要“站在开源项目上写自己的核心”，而不是 Fork 一个项目魔改到无法维护。

推荐：

```text
Open Source Runtime
        ↓
     Adapter
        ↓
Our Conversation Core
```

## 2. Pipecat

项目：
https://github.com/pipecat-ai/pipecat

用途：

- 实时语音/多模态 Agent pipeline；
- audio frame / processor 机制；
- 多种 STT / LLM / TTS 集成；
- 支持本地、WebSocket、WebRTC 等 transport；
- 方便插入自定义 Processor。

为什么适合：

我们可以把自己的：

- TurnManager
- InterruptionManager
- InitiativePolicy
- Metrics

作为独立 processor / service 接进去。

不建议：

直接把所有产品逻辑写进 Pipecat 内部源码。

## 3. Pipecat Smart Turn

项目：
https://github.com/pipecat-ai/smart-turn

用途：

> 判断用户是不是“真的讲完了”。

配合 VAD 工作。

目标解决：

- 句中停顿；
- 犹豫；
- 拖长尾音；
- 中文口语表达未完成。

第一版应优先使用，而不是自己训练 Turn Detector。

## 4. Silero VAD

项目：
https://github.com/snakers4/silero-vad

用途：

> 判断是否存在语音活动。

它只负责：

```text
speech / non-speech
```

不负责语义轮次。

## 5. LiveKit

项目：
https://github.com/livekit/livekit

第一版 PC 本地暂时不需要强依赖。

后续手机、直播、多端音频接入时非常有价值。

用途：

- WebRTC；
- 手机作为无线麦克风；
- 实时音视频房间；
- 后续多参与者。

因此架构上不要把 transport 写死。

## 6. ASR 候选

### 第一阶段

优先低延迟 Streaming ASR API。

目标：

先验证 Conversation Core。

### 后续开源候选

FunASR：
https://github.com/modelscope/FunASR

SenseVoice：
可从 ModelScope/FunASR 生态继续评估。

评估项：

- 中文错误率；
- partial result 稳定性；
- 端点检测延迟；
- 本地 GPU/CPU 资源；
- streaming 支持。

## 7. TTS 候选

第一阶段做 Provider 赛马，不锁死。

### Fish Audio

重点测试：

- 首包延迟；
- 中文自然度；
- 情绪/语气控制；
- streaming cancel。

### MiniMax Speech

重点测试：

- 中文表现；
- 角色感；
- streaming；
- 稳定声线。

### ElevenLabs Flash

用作成熟实时 TTS 基线。

重点测试：

- TTFA；
- streaming；
- 中英混合；
- 中断。

### CosyVoice

后续自部署候选。

优先价值：

- 中文；
- 可控；
- 未来减少 SaaS 依赖。

## 8. LLM

第一版统一使用：

```text
OpenAICompatibleProvider
```

我们手里 API 多，因此应该赛马：

- TTFT；
- 中文口语感；
- 遵守人格；
- 短回复能力；
- 结构化 JSON 稳定性；
- 价格。

实时 Companion 场景里：

**TTFT 通常比峰值 TPS 更重要。**

## 9. 暂时不要自己造的东西

- VAD 模型；
- Turn Detection 模型；
- ASR 模型；
- TTS 模型；
- WebRTC 协议栈。

第一版全部先用成熟组件。

我们自己的研发时间集中在：

```text
conversation/
character/
runtime coordination/
metrics/
```

## 10. 开源使用方式

每个依赖必须通过 adapter 使用。

示意：

```text
turn/
  vad_adapter.py
  smart_turn_adapter.py

providers/
  asr/
  llm/
  tts/
```

不要让业务层直接 import 供应商 SDK。
