# Qwen Realtime Plus 架构评估：端到端双工模型 vs Pipecat 分体管线

- Kind: evaluation（评估文档，非规范设计）
- Status: draft-for-decision —— 结论需一个 spike 实测后拍板，见 §6
- Date: 2026-09-22
- 评估对象：`qwen-audio-3.0-realtime-plus`（阿里云百炼），对照现有
  `Mic → Silero VAD → Smart Turn → Paraformer ASR → ConversationCore → LLM → Chunker → TTS → Playback` 分体管线
- 依据：百炼官方文档（§7 来源）、本仓库代码与 LOOM 决策记录；未实测的数据均标注"待 spike"

## TL;DR

Realtime Plus 是 OpenAI Realtime 风格事件协议的端到端双工语音模型，能力面覆盖我们管线里
ASR+LLM+TTS+Turn Detection 四段，且智能/对话节奏跑分全球第一；但它是一个**黑盒**：
不支持结构化输出、无 `conversation.item.truncate`（打断后"用户实际听到多少"无法写入服务端历史）、
分阶段延迟不可观测、轮次权默认在服务端、第三方实测 TTFA 1.5–4s（从中国直连待实测，
技术报告理论首包 435ms）。

**推荐：混合路线（方案 C）**——先做一个小 spike 实测真实 SEFA 与协议行为；
若 SEFA 进预算，Realtime Plus 作为新的 `RealtimeSession` provider 与现有分体管线并存，
ConversationCore 保持状态机/主动性/SILENCED/双历史的裁决权；
分体管线（TASK-008 继续）保留为 fallback 与对照基线。

---

## 1. Realtime Plus 能力清单

### 1.1 能做什么（官方文档确认）

| 能力 | 事实 | 来源 |
|------|------|------|
| 输入/输出模态 | Audio + Text 双向流式；上行 PCM 16kHz/16bit/mono，下行 PCM 24kHz/16bit/mono（base64 JSON 帧） | 用户指南 |
| 交互模式 | `server_vad`（声学 VAD：threshold[-1,1] 默认0.5、silence_duration_ms[200,6000] 默认800）、`smart_turn`（声学+语义融合判轮，附和声"嗯/啊"不触发轮次、不触发打断）、`push-to-talk`（手动 commit + response.create） | 用户指南/API 参考 |
| 用户语音转写 | `conversation.item.input_audio_transcription.delta/completed/failed`（有 delta 流式转写，相当于我们的 ASR partial） | 服务端事件 |
| 环境音转写 | 仅 smart_turn：非有效轮次的语音（噪声/附和）以 `ambient_audio_transcription.delta/completed` 透出，不写入对话上下文 | 用户指南/服务端事件 |
| 打断 | server_vad/smart_turn 下用户新语音自动取消在途响应（`response.done` status=cancelled，reason=turn_detected）；客户端收到 `input_audio_buffer.speech_started` 应立即清空播放缓冲；也支持 `response.cancel` 手动取消（reason=client_cancelled） | 用户指南/服务端事件 |
| 说话人增强 | 仅 smart_turn：`voiceprint_audio_urls`（≤5 个 16kHz PCM/WAV 公网 URL）注册目标说话人，屏蔽旁人/噪声 | 用户指南 |
| Function Calling | 标准 OpenAI 式 tools 注册 + `function_call_arguments.*` 事件 + `function_call_output` 写回 + `response.create` 二轮推理 | 用户指南 |
| 联网搜索 | `enable_search`（与 Function Calling 互斥） | 客户端事件 |
| 上下文管理 | `conversation.item.create/retrieve/delete`，`previous_item_id` 任意位置插入；`max_history_turns` 1–50 默认 20 | 用户指南 |
| 情绪 | `enable_speech_emotion` 默认 true，回复语音按语境动态调整语气/情感 | 客户端事件 |
| 音色 | 3.0 Plus/Flash：5 个系统音色（longanqian/longanlingxin/longanlingxi/longanxiaoxin/longanlufeng）+ 声音复刻 voice_id；3.1 Plus 增至 13 个。**注意：任务背景中"55 种音色"与官方文档不符**；voice 仅首次 session.update 可设 | 用户指南 |
| Persona | `instructions` 字段 = 系统指令，整会话生效 | 客户端事件 |
| 语种 | 11 语种 + 20 种中文方言 | 用户指南 |
| 思考模式 | 最大思维链 1024 tokens（模型信息页） | 模型信息 |
| 事件协议 | 与 OpenAI Realtime API 同构（session.update / input_audio_buffer.* / conversation.item.* / response.*） | API 参考 |

### 1.2 不能做什么（对项目重要的缺口）

- **不支持结构化输出**（模型信息页明确"结构化输出：不支持"）→ 我们的
  `AgentReply{speech, emotion, energy, should_continue}` schema 无法直接套用；
  emotion 变成模型原生语音情绪（`enable_speech_emotion`），可观测但不可枚举校验；
  `should_continue`/`NOOP` 语义需要另找载体（见 §3.3）。
- **无 `conversation.item.truncate`**：客户端事件只有 append/commit/clear/create/retrieve/delete/
  response.create/cancel。打断后服务端把"已生成的部分文本"写入 item 历史——即
  **服务端记住的是 generated，不是 heard**。我们的双历史（heard vs logical，turn-taking C5）
  在服务端侧只能用 delete+重建近似修复。
- **轮次参数在 smart_turn 模式不可调**（server_vad 的 threshold/silence_duration_ms 在
  smart_turn 下无效）；`turn_detection` 仅允许在首次发音频前（IDLE）修改，会话中不可切换模式。
- **分阶段延迟不可观测**：服务端不暴露 ASR/LLM/TTS 内部时间戳，SEFA 变成
  speech_stopped → response.audio.delta 一个黑盒数字（违反 low-latency-pipeline C5 的分阶段归因纪律）。
- **无投机执行接口**：不存在"先发 partial 让模型预热"的通道；`response.create` 在 smart_turn 下
  仅"等待用户下一轮输入时"允许调用，turn 内禁止重复触发。
- **上下文硬上限**：音频上下文最多 50 轮 / 累计 300 秒（默认 20 轮），超出自动丢弃旧音频历史；
  文本上下文 40960 tokens。**对 MVP"连续对话 ≥10 分钟"不构成阻塞**（会话不会断，只是音频记忆窗口有限），
  但快语速互怼（用例 G）可能快速消耗 50 轮窗口。
- **无本地回声消除**：WebSocket 接入无内置 AEC——与现状一致（D-007 耳机规避），不构成回退。
- **会话级行为**：连接空闲过久服务端会主动断开；`session.update` 必须在首发音频前完成。

### 1.3 三种接入协议对比（官方 Realtime API 概述页）

| 维度 | WebSocket | AOQ（AI over QUIC） | WebRTC |
|------|-----------|---------------------|--------|
| 定位 | 服务端集成、快速原型，接入门槛极低 | AI 多模态实时交互、弱网极致对抗 | 浏览器互动、传统音视频通话 |
| 弱网对抗 | 差 | 极致 | 良好 |
| AEC/降噪 | 无，客户端自理 | 内置 | 内置 |
| 端侧支持 | 全平台 | **Android / iOS / HarmonyOS** | 浏览器、移动端 |
| 鉴权 | API Key 直连 | AppServer 签发一次性 AOQ Token | Token |
| 对本项目适配 | ✅ PC Python 直接可用（websockets 已在依赖内，DashScopeASR 同款打法） | ❌ 无 Python/桌面 SDK，第一阶段出局 | ⚠️ 需引入 aiortc 等重依赖，收益是 AEC——第一阶段不必要 |

**结论：本项目若接入，唯一现实路径是 WebSocket。** URL 形如
`wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?model=<model>`，
鉴权 `Authorization: Bearer <key>`，与现有 DashScopeASR 一样需要直连绕开 Clash（F-030 同款坑）。

### 1.4 延迟、价格、限流（官方 + 第三方实测）

- **官方/技术报告**：Artificial Analysis Speech-to-Speech 综合第一；技术报告理论首包延迟
  Plus ≈435ms / Flash ≈235ms（理想条件）。
- **第三方实测（Artificial Analysis，从其测试端发起，端点在境内）**：
  - S2S Index 84.1%（#1，超 GPT-Realtime-2.1 High 的 79.1%）；
  - Speech Reasoning 99.2%（Big Bench Audio #1）；Conversational Dynamics 98.4%（Full Duplex Bench #1）；
  - **平均 Time-to-First-Audio：Plus 4.02s（一行数据显示 1.54s，口径不同）；Flash 4.16s**；
    对照 GPT-Realtime-2 High 1.14s、最快 Deepslate Opal 0.44s；
  - 实测输入音频成本约 $4.42/小时（Big Bench Audio subset 口径）。
  - ⚠️ 诚实解读：4s 含跨境网络开销与推理型题目的思考时间，不代表境内直连的对话场景；
    但即便按 1.54s 口径也**超出我们 500–800ms 的 SEFA 目标区间**。真实数字必须境内直连实测（§6 spike）。
- **价格（华北2·北京）**：输入音频 ¥40/百万 tokens；输入文本 ¥5/M；输出文本 ¥40/M；
  输出文本+音频 ¥150/M（文本不计费）。新加坡约 +20%。
- **限流**：60 RPM / 100,000 TPM（两地域同）。单人 Companion 场景够用；
  待确认：持续上行的静音音频是否计入 tokens（影响成本模型）。
- **同族模型**：`qwen-audio-3.0-realtime-flash`（速度取向，对话动力学 96.9%）、
  `qwen-audio-3.1-realtime-plus`（2026-09-20 更新，13 系统音色）——若走端到端路线应一并评估 Flash。

### 1.5 与 Pipecat 各组件的能力映射

| 分体组件（本项目） | Realtime Plus 对应 | 映射质量 |
|---|---|---|
| `turn/vad_adapter.py`（Silero） | 服务端 server_vad / smart_turn 内置 | ✅ 全包；本地 VAD 可留作最快 barge-in 触发信号 |
| `turn/smart_turn_adapter.py`（Smart Turn v3） | 服务端 smart_turn（语义判轮 + turn_invalid + 附和声豁免 + 环境音转写） | ✅ 全包且中文原生（Smart Turn v3 中文 FNR 9.26% 的短板被规避）；但参数不可调 |
| `providers/asr/`（DashScopeASR） | `input_audio_transcription.delta/completed` | ✅ partial/final 事件等价 |
| `providers/llm/`（OpenAICompatibleLLM） | 模型本体 + instructions + Function Calling | ⚠️ 有损：无结构化输出、无独立 TTFT、无赛马 |
| Text Chunker | 不需要（模型直接产音频流） | ✅ 整段消失 |
| `providers/tts/`（Fish/CosyVoice 赛马） | 内置 TTS（voice/enable_speech_emotion/声音复刻） | ⚠️ 有损：音色固定 5 选 1、无法赛马、无 cancel 契约可测（打断=整响应取消） |
| `runtime/playback.py`（StreamingPlayer） | 无对应——播放仍是本地职责 | ✅ 保留复用 |
| `runtime/mic.py` | 无对应——采集仍是本地职责 | ✅ 保留复用 |
| ConversationCore 状态机/双历史/InitiativePolicy | 服务端 turn/response 事件 + conversation.item 管理 | ⚠️ 部分映射：状态机可重建于服务端事件上；双历史/主动性/SILENCED 需要客户端侧重设计（§3.3） |
| InterruptionManager 六步 | `speech_started`→本地停播清 buffer + 服务端自动 cancelled | ⚠️ 媒体层等价；逻辑层 heard-history 精度下降（无 truncate） |
| LatencyMetrics 九时间戳 | 只剩 speech_started/speech_stopped/transcription/response.* 事件时刻 | ❌ 有损：t_asr_final→t_llm→t_tts 内部不可分 |

---

## 2. 架构对比

### 2.1 分体路线（现状）

```
Mic → Capture → Silero VAD → SmartTurn → Paraformer ASR → ConversationCore
    → LLM(OpenAI-compat) → Text Chunker → Streaming TTS → Playback
```

**优点**
- 每段可替换、可赛马（D-004/D-014 已立），单点劣化可定位（分阶段时间戳，low-latency C5）。
- ConversationCore 是全权裁决者：轮次、打断、SILENCED、主动性、双历史——这正是项目核心资产，
  所有行为可单测（TASK-005/006 已证明）。
- 结构化 AgentReply 可校验、可降级；emotion 标签可映射 TTS 情绪。
- 成本极低（§2.3）。

**缺点**
- 延迟是各段之和：预算 endpointing 200–300 + ASR final 50–150 + LLM TTFT 200–400 +
  TTS 首chunk 100–200 ≈ **550–1050ms 理论带**；实测 DashScope ASR first-event 已 785ms（TASK-007 evidence），
  SEFA 达标需要重叠执行（投机 prompt 等）全部做对。
- 集成复杂度高：四家协议、四种 cancel/重连语义、跨组件时序 bug 面大。
- 情绪链路是"标签→TTS 参数"的间接控制，天花板低于原生语音情绪。

### 2.2 端到端路线（Realtime Plus）

```
Mic → Capture ─────────────→ Realtime Plus (WS duplex) → Playback
        （服务端内置 VAD/Turn/ASR/LLM/TTS/打断/情绪）
```

**优点**
- 架构极简：一条连接替代四个 provider；无需 chunker、无需 LLM/TTS 赛马与接线；
  打断由服务端自动处理，天然无"陈旧响应"类 bug（turn-taking 失败信号之一）。
- 中文语义判轮为模型原生能力（附和声豁免 + turn_invalid + ambient 转写），
  规避 Smart Turn v3 中文 FNR 9.26% 的已知短板。
- 情绪/语气在音频内原生表达，超过"emotion 标签→TTS instruct"的表现力上限。
- 对话动力学 98.4% 全球第一（Full Duplex Bench）——正是本项目最看重的维度。

**缺点**
- **黑盒**：无分阶段延迟归因、无结构化输出、无投机执行、轮次参数不可调（smart_turn 下）。
- **裁决权上交**：轮次判定/何时回复由服务端决定；SILENCED、"该不该主动开口"
  这类我们的核心规则只能在客户端外围补救（见 §3.3），确定性下降。
- **双历史精度下降**：无 truncate，服务端记 generated 不记 heard；修复靠 item delete+重建，复杂且有窗口期。
- **延迟不确定性**：AA 实测 TTFA 1.5–4s（含跨境与重推理题），理论 435ms；
  境内直连真实值未知——这是路线决策的第一变量。
- **供应商锁定**：事件协议虽 OpenAI 同构，但 ambient/voiceprint/turn_invalid 等是 Qwen 私有扩展；
  第二家 fallback（GPT Realtime、Gemini Live）事件面不完全重合，抽象层要重新设计。
- **会话上限**：50 轮 / 300s 音频上下文、60 RPM / 100k TPM；单用户 PoC 够用，长对话记忆窗口有限。

### 2.3 成本粗估（待实测修正）

以 10 分钟连续会话为口径（用户说 ~40%、派蒙说 ~40%）：

- **分体**：Paraformer ASR ~¥0.6/hr 量级（资源包价）→ ~¥0.04；LLM qwen-flash 级别 → 分位；
  CosyVoice v3-flash ¥1/万字符，~2000 字 → ¥0.2。**合计 ≈ ¥0.2–0.5 / 10min**。
- **Realtime Plus**：上行持续流式 600s（静音是否计费待确认），AA 实测输入音频成本
  ~$4.42/hr 口径 → 10min ≈ ¥5 量级上限；输出音频 ¥150/M。**合计估 ¥2–6 / 10min**。
- 结论：**端到端约贵 5–10 倍，但绝对值仍是元/小时级，PoC 阶段不构成决策权重**；
  若未来上量则成为主要反对票。

---

## 3. 接入可行性

### 3.1 现有 Provider 抽象层能否适配？

**不能直接套进现有三接口。** ASRProvider/LLMProvider/TTSProvider 是三个单向流抽象
（audio→events / messages→tokens / chunks→audio），Realtime Plus 是一条双工会话：
音频上行与事件/音频下行在同一连接、生命周期绑定、取消语义跨段共享。硬拆成三个 adapter 会丢掉
"一次 speech_started 同时取消 LLM 生成和 TTS 推送"这类天然一致性，等于自己重缝一个分体。

**正确的适配形状**是新增第四类抽象（示意，非实现）：

```python
class RealtimeSessionProvider(ABC):
    async def send_audio(self, chunk: bytes) -> None
    def events(self) -> AsyncIterator[RealtimeEvent]   # speech_started/stopped,
        # transcription.delta/completed, ambient_transcription, response.audio.delta,
        # response.done(cancelled/completed), error …
    async def create_response(...) -> None   # 主动开口 / function_call_output 二轮推理
    async def cancel_response(self) -> None
    async def close(self) -> None
```

`RealtimeEvent` 映射进现有 EventBus 事件面（USER_TURN_STARTED / ASR_PARTIAL / ASR_FINAL /
AGENT_INTERRUPTED / PLAYBACK 相关），ConversationCore 继续消费同一套事件——
这样 core 的事件契约不变，state machine / TurnManager / metrics 不需要分叉。

**另一条更省力的路**：Pipecat 1.11 内置 `pipecat.services.openai.realtime`
（`OpenAIRealtimeLLMService`，`base_url` 可配）。Qwen 的 WS 事件协议与 OpenAI Realtime 同构，
把 base_url 指到 `wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime` 大概率能跑
——但 Qwen 私有事件（ambient_transcription、turn_invalid、voiceprint）会被 OpenAI adapter 忽略或报错，
smart_turn 专属语义拿不到；用作快速 spike 可以，作正式接线需自研事件映射层。
（已核实：`.venv/.../pipecat/services/openai/realtime/llm.py` `base_url` 参数存在。）

### 3.2 需要改哪些接口/约定

1. **新增 `src/providers/realtime/` 目录与 RealtimeSessionProvider 抽象**（改动量 ≈ 一个 TASK-002 级）。
2. **EventBus 事件映射表**：`input_audio_buffer.speech_started` → USER_TURN_STARTED + 打断媒体信号；
   `speech_stopped` → 轮次边界信号（smart_turn 的 reason=turn_invalid → 不产生 TURN_COMPLETE）；
   `input_audio_transcription.delta/completed` → ASR_PARTIAL/ASR_FINAL；
   `ambient_audio_transcription.*` → 新增 AMBIENT 事件（可做环境感知/backchannel 观测）；
   `response.audio.delta` → 播放数据面；`response.done(cancelled)` → AGENT_INTERRUPTED 账本。
3. **LatencyMetrics 降级**：九时间戳 → 可观测子集
   （speech_stopped / transcription.completed / response.created / first audio.delta / playback_start /
   interrupt_detected / playback_stopped），SEFA 退化为单段黑盒。建议同时记录
   `speech_started→response.created` 作为服务端"思考时长"代理指标。
4. **AgentReply/结构化输出**：Realtime 路线下不存在。persona 改由 `instructions` 承载；
   emotion 标签从"控制信号"降级为"可观测性标签"（如需保留，只能 modalities=["text"] 额外打一轮
   text-only 推理做旁路标注——昂贵且割裂，不建议）。
5. **heard-history 修复**：取消的响应其 partial 文本已在服务端历史里；要表达"用户只听到 X"，
   需 `conversation.item.delete` 该 assistant item 再以 `conversation.item.create` 插入截断文本+
   打断标记。可在客户端本地仍维护双历史，服务端侧做尽力修复。
6. **音频格式对齐**：上行 16kHz mono int16 与 MicCapture 一致；下行 24kHz 与现有
   StreamingPlayer(48k?16bit) 需确认/重采样——playback 层改动小。
7. **网络**：与 DashScopeASR 同款 `proxy=None` 直连（Clash +1.9s/连接坑，F-030）；
   URL 含 WorkspaceId，.env 需新增配置项。

### 3.3 需要保留 Pipecat 分体管线作为 fallback 吗？

**要，且成本已被摊薄。** 理由：

- 三条硬性能力缺口（结构化输出、分阶段延迟归因、heard-history 精度）是 Realtime Plus
  **协议层面就没有的**，不是参数问题；分体管线是这些能力的唯一载体。
- TASK-008 的 TTS 双 adapter 即使 Realtime 路线胜出也有残留价值：
  acknowledgment cue（"嗯……让我想想"）固定短语可走廉价 TTS；Realtime 断连时的降级出口。
- Provider 抽象的意义本来就是赛马与逃生门；删掉等于把 D-004/D-014 推翻一半。

但"保留"应定义为**可切换的备份路径，而非双活**：同一时刻只跑一条链路，否则两份
turn-taking 语义会打架（D-012 已经踩过"双打断路径"的坑）。

### 3.4 混合架构是否可行？

可行，且有两种粒度：

**(a) 会话内混合——推荐形态**：Realtime Plus 用 `smart_turn` 模式接入，
本地 ConversationCore 保持裁决层。服务端 turn 判定作为"信号"进入 TurnManager
（而不是直接驱动状态机），与我们既定"ASR/判轮只是信号源"的权责边界一致
（streaming-asr-zh C3 同款思想）。关键缺口与对策：

| 需求 | 机制 | 可行性 |
|---|---|---|
| 派蒙主动开口（InitiativePolicy） | smart_turn 等待用户下一轮时允许 `response.create`；可先 `conversation.item.create` 注入引导文本再 create | ✅ 官方支持 |
| SILENCED（"闭嘴两分钟"） | **上行静音门控**：SILENCED 期间停止 `input_audio_buffer.append` → 模型收不到语音自然不响应；唤醒词"派蒙"检测走本地信号链 | ✅ 可行；但静音期间服务端也不见 ambient 转写 |
| SILENCED 中唤醒/退出检测 | 需一条本地/旁路 ASR 信号链——`DashScopeASR`（TASK-007 已完成）正好复用为信号源 | ✅ 已有资产，仅 SILENCED 窗口内计费一路 ASR |
| 模型在不该响应时响应了 | `response.cancel`（客户端取消）；代价是白付一轮生成 tokens | ⚠️ 兜底手段，非常态 |
| backchannel 观测 | `ambient_audio_transcription.*` 透出附和声文本——比本地"VAD 立即切"更准 | ✅ 直接可用，反哺 turn-taking C4 的 backchannel 容忍增强 |
| 打断媒体层 | `speech_started` → 本地 playback.stop() 清 buffer（现有六步的前两步）；逻辑层取消已由服务端完成 | ✅ 等价，且跨段一致性由服务端保证 |
| 打断后 heard 历史 | 本地 playback.position_seconds 已知播到哪 → 本地双历史照旧精确；服务端侧 delete+create 尽力修复 | ⚠️ 有损但可工作 |

**(b) 链路级混合（运行期择路）**：Realtime 链路与分体链路并存于同一抽象下，
按配置/健康度选路。这是 (a) 落地后的自然结果，不额外设计。

**不可行的混合**：在 server_vad/smart_turn 模式下又想保留本地 SmartTurn 做裁决——
两套判轮必然抢话/死等；以及想用结构化输出驱动 Realtime——协议不支持，死心。

---

## 4. 推荐方案

### 方案 A：纯分体（维持现状路线）

按 tasks.json 原样推进 TASK-008→012。

- 工程量：剩余 4–5 个已定义 Task 的量（TTS 双 adapter、persona、组装、initiative、验收）。
- 优点：核心资产完全自控；所有验收标准（九时间戳、双历史、结构化输出）不变形。
- 缺点：SEFA 达标有真实风险（ASR 单项已实测 785ms，达标需投机执行全做对）；
  四家供应商运维面大；情绪表达天花板低于端到端。
- 适用：Realtime Plus 境内实测 SEFA >800ms，或 smart_turn/上下文上限被证伪时。

### 方案 B：纯端到端（Realtime Plus 替换 ASR+LLM+TTS+Turn）

ConversationCore 退化为"服务端事件→状态机/播放/门控"的薄适配层。

- 工程量：Realtime adapter + 事件映射 + playback 对齐 ≈ 1.5–2 个 Task 量；
  但要**返工设计文档**（conversation-core 双历史降级、metrics 九时间戳作废、
  persona 结构化输出废弃）+ TASK-004/005/006 部分资产闲置（VAD/SmartTurn/六步打断只剩
  本地媒体层那半）。净工程量≈省 2 个 Task、废 1.5 个 Task 的设计投资。
- 优点：代码最少、链路最短、对话节奏跑分全球第一、情绪原生。
- 缺点：**放弃本项目核心资产的一半权责**（轮次/主动性/SILENCED 变成外围补救），
  结构化输出与分阶段观测永久消失；延迟、成本、限流三个未知数全压在单一供应商上；
  MVP 定义里"可替换 ≥2 LLM + ≥2 TTS"直接失效，验收文档要重写。

### 方案 C：混合（Realtime 链路 + 分体链路并存，ConversationCore 统一裁决）★ 推荐

新增 `providers/realtime/qwen.py`（smart_turn 模式）+ 事件映射进 EventBus；
ConversationCore 不变；分体管线按原计划完成作为基线与 fallback；
两条链路在同一 latency log 口径下 A/B。

- 工程量：
  - Spike（前置，见 §6）：0.5–1 个 Task 量，一个脚本，不动抽象；
  - RealtimeSessionProvider 抽象 + Qwen adapter + 事件映射 + 指标降级：≈1.5 个 Task；
  - SILENCED/主动性门控（上行静音 + response.create/cancel + DashScopeASR 信号链复用）：≈0.5–1 个 Task；
  - 合计 ≈2.5–3 个 Task，**且 TASK-008 照旧**。
- 优点：不赌单一未知数；核心资产权责不丢；拿到端到端模型的中文判轮/附和声豁免/原生情绪
  三大利好；分体管线成为永久对照组与逃生门；MVP 验收口径大部分不变。
- 缺点：工程量最大；要维护两套链路；双历史/指标降级部分设计债仍在。

**推荐方案 C，但先 spike 后接线**：当前唯一真正阻塞决策的未知数是
**境内直连真实 SEFA**（理论 435ms vs AA 实测 1.5–4s 的巨大方差）和
**smart_turn 行为细节**（ambient 转写覆盖率、turn_invalid 判定质量）。
这两个用一个小脚本实测就能消除，不值得在数据缺失时改写路线图。

---

## 5. 对 Task 路线图的影响（方案 C 落地前提下）

| Task | 调整 |
|------|------|
| TASK-003（blocked，LLM 赛马） | 不变。分体链路仍是基线；Realtime Plus 若胜出，本任务数据仍用于"分体 fallback 的 LLM 选型"。可顺手加一行"RT Plus 整体轮次耗时"作对照，不阻塞。 |
| TASK-008（TTS 双 adapter） | **继续，且不应因本评估暂停**。理由：分体基线需要完整链路才能对比 SEFA；TTS adapter 保留 cue 短语与降级出口价值。若 spike 后明确走 Realtime 主线，可把验收从"双 adapter 赛马"降为"单 adapter 可用"以省量（改验收需走 loom decision）。 |
| **新增 TASK-008b（spike）** | Realtime Plus WS 冒烟：连 cn-beijing，server_vad+smart_turn 各跑 ≥10 轮，实测 speech_stopped→first audio delta 分布、transcription delta/completed 时序、speech_started→服务端 cancelled 时序、上行静音门控有效性、300s/50轮上限行为、静音上行是否计费。产出与 TASK-003 同格式的对比表。 |
| TASK-009（persona） | 核心不变（instructions 承载 persona prompt）。Realtime 路线下"结构化输出 schema 稳定"这条验收不适用于 RT 链路——persona 验收对 RT 改为"语音/文本输出符合人格人工评分"；emotion 标签集保留为分体链路专用。建议验收加 provider-conditional 措辞。 |
| TASK-010（端到端组装） | 分体主链不变；追加 RT 链路组装分支（RealtimeSessionProvider→EventBus→playback），metrics 对 RT 链路用降级时间戳集。若 spike 显示 SEFA 显著优于分体，RT 升主链、分体降 fallback——该切换只是一个配置路由，不应 fork core。 |
| TASK-011（InitiativePolicy + SILENCED） | **改动最大**。分体语义不变；RT 链路下需新增：上行静音门控（SILENCED 入口）、DashScopeASR 信号链（唤醒词/规则分类）、`response.create` 主动开口通道、`response.cancel` 兜底。验收标准按链路分条件。 |
| TASK-012（MVP 验收） | 用例 A–G 全部仍然有效（这正是设计好的验收），延迟目标按实测修正；"≥2 LLM + ≥2 TTS 可替换"一条对 RT 主线需重新措辞（可改为"≥2 条生成链路可切换"）。 |

**可删除**：无。所有已完成 Task（001–007）的产物在方案 C 下全部继续服役
（VAD/SmartTurn/六步打断服务于分体链路与 RT 的本地媒体层；DashScopeASR 复用为 SILENCED 信号链）。

**可合并**：TASK-008b spike 可并入 TASK-008 作为第 4 条验收前置，但建议独立——
它是路线决策的输入，不是 TTS adapter 的产出。

**需重写**：仅当未来拍板"纯端到端"时，conversation-core.md / mvp-evaluation.md 的
双历史与九时间戳条款才需要改——本评估不建议现在改。

---

## 6. 决策前置：spike 清单（消除未知数的最小实验）

在改任何抽象层之前，一个 ~150 行脚本可回答：

1. cn-beijing 直连，smart_turn 模式下 speech_stopped → `response.audio.delta` 首包的
   p50/p95 是多少？（对照分体 SEFA 预算 500–800ms）
2. `input_audio_transcription.delta` 的更新节奏能否支撑 prompt 预构造级别的提前量？
3. SILENCED 场景：停止 append 后服务端是否零响应？恢复后上下文是否干净？
4. `speech_started` 到 `response.done(cancelled)` 的间隔 = 服务端打断确认延迟？
5. ambient_audio_transcription 对"嗯/啊/旁边人说话"的覆盖率与误伤率？
6. 静音上行是否计 audio tokens（成本模型）？
7. 单连接可持续时长 / 空闲断开阈值 vs 10 分钟 MVP 会话？

数据到手后再走 `loom decision` 拍板主链；本文件届时更新 Status。

## 7. 来源

- 模型信息/价格/限流：`help.aliyun.com/zh/model-studio/qwen-audio-3-0-realtime-plus`
- 用户指南（协议、模式、音色、上下文、打断、上下文管理、说话人增强、环境音转写）：
  `alibabacloud.com/help/zh/model-studio/qwen-audio-realtime-user-guides`（更新于 2026-09-21）
- 协议对比：`help.aliyun.com/zh/model-studio/realtime-api-overview`
- 服务端事件：`help.aliyun.com/zh/model-studio/qwen-audio-realtime-server-events`
- 客户端事件：`help.aliyun.com/zh/model-studio/fun-audiochat-client-events`
- 第三方实测：Artificial Analysis Speech-to-Speech leaderboard + AlphaSignal 报道
  （TTFA 4.02s/1.54s 两口径、Index 84.1%、Reasoning 99.2%、Dynamics 98.4%、理论首包 435/235ms）
- TTS/ASR 价格锚点：cosyvoice-v3-flash ¥1/万字符、Paraformer 资源包 ~¥0.6/hr（百炼定价页）
- 本仓库：`02_SYSTEM_ARCHITECTURE.md`、`06_MVP_AND_EVALUATION.md`、`.loom/DECISIONS.md`（D-002–D-014）、
  `.loom/tasks.json`、`src/providers/{asr,llm,tts}/base.py`、`src/providers/asr/dashscope.py`、
  `.loom/capabilities/{turn-taking,low-latency-pipeline,chinese-tts-eval,streaming-asr-zh}/capability.md`、
  `.venv` 内 pipecat 1.11.0 `services/openai/realtime/llm.py`（base_url 可配）

## 8. 与任务背景陈述的出入（诚实记录）

- "55 种音色"：官方文档 3.0 Plus/Flash 仅列 5 个系统音色 + 声音复刻；3.1 Plus 13 个。
  "55"未见于官方来源，按 5+复刻记录。
- "延迟全球第一"：准确说法是 S2S 综合指数/推理/对话动力学第一；**TTFA 实测不是第一**
  （1.5–4s 区间，榜内偏慢），技术报告理论值 435/235ms。
- "三种轮次模式"：准确为 server_vad / smart_turn / push-to-talk（manual）。
