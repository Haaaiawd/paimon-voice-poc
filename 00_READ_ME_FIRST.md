# Paimon Voice PoC — Read Me First

> 这是一套用于项目接手、AI 协作和后续开发初始化的“最小完整文档”。
> 当前阶段只做 **电脑端实时语音对话**，不做 3D、不做 X4 Air、不做直播合成。

## 一句话定义

做一个运行在电脑上的实时语音 Companion：用户对着麦克风说话，**派蒙**能以很低延迟自然接话、被打断、偶尔主动插嘴，并保持稳定人格与短期上下文。

它不是普通“语音问答助手”。

我们更在意：

- 什么时候该说；
- 什么时候不该说；
- 被打断后怎么继续；
- 怎样避免客服式一问一答；
- 怎样让互动有“熟人拌嘴”的节奏；
- 怎样把端到端延迟压低。

## 当前阶段不做

- 3D 模型、骨骼、动作；
- X4 Air 接入；
- OBS / 直播平台；
- 手机 App；
- 视觉理解；
- 长期记忆；
- 自训练 ASR / TTS / LLM。

这些都放在 Voice Core 成立以后。

## 阅读顺序

1. `01_PRODUCT_SCOPE.md`
2. `02_SYSTEM_ARCHITECTURE.md`
3. `03_CONVERSATION_CORE.md`
4. `04_TECH_STACK_AND_OPEN_SOURCE.md`
5. `05_PAIMON_PERSONA.md`
6. `06_MVP_AND_EVALUATION.md`
7. `07_DECISIONS_AND_OPEN_QUESTIONS.md`

## 已经拍板的核心决定

- 第一版：PC 本地运行。
- 输入：电脑麦克风。
- 角色：派蒙。
- 重点：低延迟 + 自然轮次 + 打断 + 主动插话。
- 框架方向：优先研究/使用 Pipecat。
- Turn Detection：Silero VAD + Pipecat Smart Turn。
- LLM：Provider 抽象，优先接 OpenAI-compatible API，按 TTFT 赛马。
- TTS：Provider 抽象，优先选择“低首包延迟 + 中文自然度 + 情绪表现”。
- 第一阶段 TTS 候选：Fish Audio、MiniMax、ElevenLabs；CosyVoice 作为后续自部署候选。
- 第一阶段不为“纯开源”牺牲体验；核心逻辑必须由我们自己掌握。
- 项目的核心代码不是 ASR/TTS/LLM adapter，而是 Conversation Core。

## 项目真正的核心资产

未来即便换掉所有模型，下面这些仍应保持稳定：

- Turn Manager
- Interruption Manager
- Initiative Policy
- Conversation State Machine
- Persona / Behavior Policy
- Short-term Context
- Latency Metrics

这是本项目最需要自己写、自己理解、自己迭代的部分。

## 给接手 AI 的提醒

不要把任务重新扩成“通用语音 Agent 平台”。

先完成：

**用户说话 → 正确判断轮次结束 → 派蒙快速回复 → 用户可随时打断 → 派蒙知道自己被打断 → 继续自然对话。**

如果这个闭环不自然，其余功能全部没有意义。
