# Fish Audio 流式 TTS 协议事实

Source: docs.fish.audio（WebSocket TTS Streaming、Realtime Streaming、Realtime TTS v3 协议）

WebSocket 端点 `wss://api.fish.audio/v1/tts/live`，MessagePack 二进制帧，握手带
`Authorization: Bearer <key>` 与 `model` header（s1 / s2-pro）。

关键机制：
- HTTP streaming（`tts.stream`）：已有完整文本时 TTFA 最低，最简单；
- WebSocket（`tts.stream_websocket`）：文本还在生成时（LLM token 流）用，开口早于句子完成；
- 服务端会缓冲文本以攒够自然度上下文——想强制立刻生成（句尾/轮次末）要发 FlushEvent；
- `latency="balanced"` 是默认也是最低 TTFA 档，voice agent 场景就用它；
- Realtime TTS v3 协议是 provider-neutral 的：Fish/MiniMax 共享同一套 camelCase 事件，
  换引擎只改 modelId——本项目 TTSProvider 抽象可以直接参考这套事件面设计 cancel/flush 语义。

对项目的含义：Text Chunker 的职责可以变薄——Fish 服务端自己做缓冲攒自然度，
但我们仍需在语义边界（句尾）发 flush，否则它会为等更多上下文而增加 TTFA。
