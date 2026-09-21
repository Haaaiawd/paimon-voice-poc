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
