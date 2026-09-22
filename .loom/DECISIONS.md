# Decision History

Current truth belongs in PROJECT.md and linked design documents. This file preserves consequential superseding decisions.

## D-001: 产品范围：PC 纯语音 Companion

- Current decision: 第一阶段只做 PC 本地实时语音对话，角色为派蒙；不做 3D、直播、手机、视觉。
- Rationale: 先验证 Voice Core 闭环（轮次/打断/延迟/人格），扩展项放在 Voice Core 成立之后。
- Source: conversation
- Supersedes: none
- Affects: 01_PRODUCT_SCOPE.md, .loom/PROJECT.md
- Recorded: 2026-09-21T12:48:55.125Z

## D-002: Runtime：Python + Pipecat

- Current decision: 使用 Pipecat 作为实时语音 pipeline 底盘，自定义组件以独立 processor/service 接入，不改 Pipecat 源码。
- Rationale: 站在开源项目上写自己的核心，而不是 fork 魔改到无法维护。
- Source: conversation
- Supersedes: none
- Affects: 04_TECH_STACK_AND_OPEN_SOURCE.md, .loom/PROJECT.md
- Recorded: 2026-09-21T12:48:55.126Z

## D-003: Turn Detection：Silero VAD + Pipecat Smart Turn

- Current decision: VAD 用 Silero 判断有无语音，Smart Turn 判断轮次是否真正结束；不自训练模型。
- Rationale: 停顿不是完成；中文口语的句中停顿需要语义级轮次判断，成熟组件优先。
- Source: conversation
- Supersedes: none
- Affects: 02_SYSTEM_ARCHITECTURE.md, 04_TECH_STACK_AND_OPEN_SOURCE.md
- Recorded: 2026-09-21T12:48:55.131Z

## D-004: LLM：Provider 抽象 + OpenAI-compatible 优先

- Current decision: 统一 OpenAICompatibleProvider 接口，多 API 赛马；最重要指标是 TTFT 而非峰值 TPS。
- Rationale: 实时 Companion 场景首 token 延迟决定体验；手里 API 多，赛马选优。
- Source: conversation
- Supersedes: none
- Affects: 04_TECH_STACK_AND_OPEN_SOURCE.md
- Recorded: 2026-09-21T12:48:55.133Z

## D-005: TTS：第一阶段云端赛马

- Current decision: Fish Audio / MiniMax / ElevenLabs 至少实测两家，用同一组句子对比 TTFA、中文自然度、情绪、取消速度；CosyVoice 作为后续自部署候选。
- Rationale: 不为纯开源牺牲第一阶段体验，但保留自部署路径减少 SaaS 依赖。
- Source: conversation
- Supersedes: none
- Affects: 04_TECH_STACK_AND_OPEN_SOURCE.md, 06_MVP_AND_EVALUATION.md
- Recorded: 2026-09-21T12:48:55.134Z

## D-006: ASR：第一阶段云端 Streaming API

- Current decision: 第一阶段优先低延迟 Streaming ASR API，暂不锁定具体 provider；FunASR/SenseVoice 后续评估。
- Rationale: 当前重点是验证 Conversation Core，不是证明 ASR 能本地跑。
- Source: conversation
- Supersedes: none
- Affects: 04_TECH_STACK_AND_OPEN_SOURCE.md
- Recorded: 2026-09-21T12:48:55.135Z

## D-007: 音频输出：第一阶段耳机，跳过 AEC

- Current decision: 开发第一阶段戴耳机规避回声；产品化后再引入 WebRTC AEC / loopback / double-talk protection。
- Rationale: AEC 复杂度高，第一阶段不应阻塞核心闭环验证。
- Source: conversation
- Supersedes: none
- Affects: 01_PRODUCT_SCOPE.md, 07_DECISIONS_AND_OPEN_QUESTIONS.md
- Recorded: 2026-09-21T12:48:55.136Z

## D-008: 打断策略：第一版不真正打断讲话中的用户

- Current decision: 主动插话限定在用户轮次结束、长沉默、明显留白等场景；暂不做 full-duplex overlap。
- Rationale: 这是体验最容易失控的地方，先收敛再实验。
- Source: conversation
- Supersedes: none
- Affects: 03_CONVERSATION_CORE.md, 07_DECISIONS_AND_OPEN_QUESTIONS.md
- Recorded: 2026-09-21T12:48:55.137Z

## D-009: TTS：第一轮单一 Fish Audio

- Current decision: 第一轮只接 Fish Audio，不做多家赛马；若实测 TTFA/中文表现不达标再补 MiniMax 等对比。
- Rationale: 减少第一阶段变量，优先打通闭环；Fish 中文表现本身强。
- Source: conversation
- Supersedes: D-005
- Affects: 04_TECH_STACK_AND_OPEN_SOURCE.md, 06_MVP_AND_EVALUATION.md
- Recorded: 2026-09-21T13:23:15.015Z

## D-010: ASR：锁定阿里云百炼流式识别

- Current decision: 第一轮 ASR 用阿里云百炼/通义听悟流式 API（DashScope），key 申请中。
- Rationale: 国内直连低延迟，中文流式稳定，与通义千问 LLM 复用同平台 key。
- Source: conversation
- Supersedes: D-006
- Affects: 04_TECH_STACK_AND_OPEN_SOURCE.md
- Recorded: 2026-09-21T13:23:15.018Z

## D-011: 开发环境：Windows 原生 + Docker 打包

- Current decision: 语音闭环跑在 Windows 原生 Python 3.12（venv），不用 WSL2 跑音频；Docker 用于依赖打包/可复现环境。
- Rationale: WSL2 麦克风/音频走 WSLg PulseAudio，低延迟场景有坑；Windows 音频栈最直接。
- Source: conversation
- Supersedes: none
- Affects: .loom/STRUCTURE.md, 07_DECISIONS_AND_OPEN_QUESTIONS.md
- Recorded: 2026-09-21T13:23:15.019Z

## D-012: Pipecat 原语与自研 Core 的权责边界

- Current decision: 判定原语（VAD/SmartTurn/帧事件）用 Pipecat；状态机、InitiativePolicy、双历史、打断处置归 src/conversation/ 自研。Pipecat 内置 interruption 只当媒体层信号源，打断路径唯一，禁止双路径并行。
- Rationale: Keeper 指出 TASK-010 集成时存在 Pipecat 默认中断逻辑与 InterruptionManager 并行生效的双打断风险；明确边界防止接线时两套机制打架。
- Source: conversation
- Supersedes: none
- Affects: .loom/design/conversation-core.md, .loom/design/system-architecture.md
- Recorded: 2026-09-21T13:59:00.469Z

## D-013: SILENCED 的生产者与退出规则

- Current decision: SILENCE_REQUESTED 由 Conversation Core 内规则分类器基于 ASR_FINAL 文本产出（闭嘴/别说话/安静类关键词，不走 LLM）；SILENCED 中 ASR 持续转写，命中'派蒙'直呼/超时/高优事件退出。
- Rationale: doc 03 有 SILENCE_REQUESTED 事件但未指定生产者；规则分类器省一次 LLM 往返且确定性可测。
- Source: conversation
- Supersedes: none
- Affects: .loom/design/conversation-core.md, .loom/tasks.json
- Recorded: 2026-09-21T13:59:00.470Z

## D-014: TTS 改为双 adapter 实测赛马

- Current decision: 第一轮同时实现 Fish（s2.1-pro-free 免费层）与百炼 cosyvoice-v3-flash 两个 TTS adapter，真实管线内对比热 TTFA 与中文自然度后定主力；败方保留为备选路径。
- Rationale: Fish 免费但跨境延迟实测 0.9–4s（HTTP）；百炼境内直连 ~0.6–0.8s 含握手且支持 Instruct 情绪控制。两边都有实测依据，用管线数据裁决而非拍脑袋。
- Source: conversation
- Supersedes: D-009
- Affects: .loom/design/tech-stack.md, .loom/design/conversation-core.md, .loom/capabilities/chinese-tts-eval/
- Recorded: 2026-09-21T15:38:55.611Z

## D-2026-09-22-8b5z: TTS 赛马裁决：百炼 cosyvoice-v3-flash 胜出为主力 TTS，Fish s2.1-pro-free 保留为第二家可插 adapter（D-014 落地）。实测热 TTFA（WS 常驻/预热连接，10 轮×8 派蒙域测句）：bailian mean 1021ms / p50 976ms / p95 1262ms；fish mean 1137ms / p50 1090ms / p95 1364ms。取消成功率两家均 100%（5/5，零残音）。bailian 另胜在境内直连稳定性与 v3 Instruct 情感控制通道（emotion 标签可映射）；fish 保留免费层成本优势但跨境延迟更高且 v1 协议一会话一连接（靠预热保热 TTFA）。注意：bailian cancel() 在文本已全部提交的最坏情况下需排空残余音频，实测 ~1.1s；音频对消费方立即停推，等待只发生在返回前清 socket——barge-in 链路若要求 cancel 秒回，可改走关 socket 快路径（牺牲连接复用）。接口不锁死：TTSProvider 抽象不变，败方 adapter 仍可随时替换或并存。

- Changed: data/tts_benchmark/latest.json, data/tts_benchmark/latest.md, src/providers/tts/fish_audio.py, src/providers/tts/bailian_cosyvoice.py, .loom/design/tech-stack.md
- Affected tasks: TASK-008
- At: 2026-09-22T01:37:42.023Z

## D-2026-09-22-6thz: Smart Turn 中文 incomplete 兜底从 3.0 秒收紧为 1.2 秒；用户明确选择实时响应优先。正常模型判定路径仍由 VAD 0.2 秒 + Smart Turn 驱动，1.2 秒只用于模型误判 incomplete 的最坏兜底。继续记录 model/fallback 来源与抢话率；若真实长思考场景抢话明显，回调到 1.8 秒。反向打断用户暂不扩展，维持 D-008。

- Changed: src/turn/smart_turn_adapter.py, src/runtime/audio_demo.py, src/runtime/simulated.py, tests/test_turn_adapters.py, .loom/design/system-architecture.md, .loom/capabilities/turn-taking/capability.md
- Affected tasks: TASK-004, TASK-010
- At: 2026-09-22T06:44:28.247Z

## D-2026-09-22-q1bn: 实时语音默认 LLM 从 qwen-turbo 切换为 qwen-flash，并将浏览器回复播放从整句 WAV 缓冲改为首个 PCM 块到达即用 Web Audio 连续调度。依据同一派蒙结构化负载实测：qwen-flash TTFT mean 609ms、结构化 2/2；qwen-turbo 1678ms，qwen3.8-flash 7124ms，qwen3.7-flash 12441ms。WS audio.chunk 已是真流式，前端不再等待 reply.final；用户开口仍立即 stopAll 实现本地 barge-in。

- Changed: src/runtime/main.py, src/runtime/ws_gateway.py, scripts/bench_llm.py, .env.example, frontend/src/audio/ReplyPlayer.ts, frontend/src/components/Waveform.tsx, frontend/src/App.tsx, .loom/design/tech-stack.md, .loom/design/FRONTEND_DEMO_DESIGN.md
- Affected tasks: TASK-003, TASK-010, TASK-017, TASK-018
- At: 2026-09-22T07:27:24.467Z
