# Pipecat Smart Turn 机制与参数边界

Source: Pipecat 官方文档 Smart Turn Overview (docs.pipecat.ai) + pipecat-ai/smart-turn repo

Smart Turn v3 是 ONNX 本地推理模型，CPU 上 <100ms，权重随 pipecat-ai 包内置。工作方式：
VAD 检测到停顿 → Smart Turn 分析该轮**最近 8 秒音频**（max_duration_secs=8，超出用滚动窗口）
判定 complete/incomplete → 若判 incomplete 但静音持续超过 SmartTurnParams.stop_secs（默认 3.0s），
fallback 为 complete。pre_speech_ms=500 保留语音前缓冲。

关键工程事实：
- Smart Turn 依赖 VAD，官方建议 VAD `stop_secs` 设 0.2s（短停顿就触发分析，而不是等长静音）；
- VAD 的 stop_secs 与 SmartTurnParams.stop_secs 是两个不同参数，前者控制"停顿事件何时产生"，
  后者控制"incomplete 判定后多久兜底成 complete"；
- Pipecat ≥0.0.102 起 TurnAnalyzerUserTurnStopStrategy + LocalSmartTurnAnalyzerV3 是默认
  stop strategy；turn start 默认 = VAD 检测或 transcript 到达；
- stop strategy 可组合：SpeechTimeout（固定静音窗）/ TurnAnalyzer（模型）/
  FilterIncomplete（LLM 判语义完整）/ 自定义；user_turn_stop_timeout 默认 5s 兜底。

专家判断点：模型判定决定"自然度上限"，stop_secs fallback 决定"最坏等待"，两者要分别调。
