# 延迟预算：800ms 规则与分阶段拆解

Source: Twig "Latency Budgets for Voice AI Agents"、Picovoice Voice UX ch.3、
ElevenLabs voice-agent-latency blog、moss.dev latency budget breakdown、Twilio core latency guide

人类对话轮次间隔中位数约 200ms（Stivers et al. 2009, PNAS，跨 10 语言）；>800ms 显尴尬，
>1500ms 感觉坏掉。工程目标：p50 首音频 <800ms，p95 <1500ms。

延迟是**预算不是单点指标**——端到端是各阶段之和，只优化一段就只移动那一段的份额：
| 阶段 | p50 预算 |
|------|---------|
| End-of-turn 判定 | 200–300ms |
| ASR final | 50–150ms |
| LLM TTFT | 200–400ms |
| TTS 首 chunk | 100–200ms |
| 网络 jitter | 剩余 |

两个结构性事实：级联云端 agent 每个远程阶段都付一次网络往返（这是模型内优化无法消除的项）；
阶段可以重叠——推理可在轮次确认前开始，合成可在推理完成前开始。串行执行付全部之和，
重叠执行接近最长一段。LLM TTFT 和 endpointing 是最大的两个单项。

感知延迟可独立于实测延迟管理：重叠阶段、提前开口、慢轮次发 acknowledgment 都改变
"感觉多快"而不改变组件速度。
