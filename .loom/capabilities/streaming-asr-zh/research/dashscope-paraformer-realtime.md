# DashScope Paraformer 实时识别协议与参数

Source: 阿里云百炼文档（Paraformer 实时语音识别 WebSocket API / 客户端事件 / API 参考）

WebSocket duplex 协议：`wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference`
（或旧 dashscope.aliyuncs.com 域），握手阶段 Authorization: Bearer <key>。
流程：run-task → task-started → 持续发二进制单声道音频 → 持续收 result-generated
→ finish-task → task-finished → 关连接。

关键参数（paraformer-realtime-v2）：
- 任意采样率（v1 只支持 16000，8k 型号只支持 8000）；音频须单声道；
- `semantic_punctuation_enabled`：true=语义断句（准但慢，适合会议转写），
  false=VAD 断句（默认，延迟低，适合交互）——本项目用 false；
- `max_sentence_silence`：VAD 断句静音阈值，默认 1300ms，范围 200–6000——
  这是服务端自己的 endpointing，与本项目的 Smart Turn 是两套判定，不能混用；
- `disfluency_removal_enabled` 语气词过滤（派蒙场景的用户口语里"嗯/那个"是否应该去掉
  值得斟酌——ASR 文本给 LLM 用时去掉更干净，但语气词本身携带犹豫信号）；
- `language_hints` 可指定 zh；`heartbeat` 保持静音期连接；
- DashScope Python SDK 有 Recognition 双向流式封装（sendAudioFrame + 回调），
  也暴露 first_package_delay / last_package_delay 指标。

注意：result-generated 里的句子边界是服务端的断句判定，不等于对话轮次边界。
