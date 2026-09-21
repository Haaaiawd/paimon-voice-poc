# 低延迟语音 pipeline 与 SEFA 预算

> One dossier covers one recognizable professional field. Keep UI/UX, visual art direction, game
> design, psychology, biology, security, and other fields separate when their evidence and judgments
> differ. Put cross-field synthesis in the affected design document, not in a hybrid capability title.
> The kernel of a capability is a decision tree with conditional branches that captures how an expert
> in this field thinks — not a fixed step list or a declarative stance document. It is a resource for
> the Agent to use, not a script it must follow. When evidence points outside the tree, trust the
> evidence and update the tree.

## Field identity and boundary

端到端响应延迟工程：把"用户说完→agent 出声"的间隙作为分阶段预算来管理。
覆盖 SEFA 预算分配、streaming/重叠架构、慢轮次兜底、缓冲与传输取舍、指标纪律。
不含轮次判定正确性（turn-taking）、具体 provider 选型（chinese-tts-eval / streaming-asr-zh）。

## Project scenario

本地音频 + 云端 ASR/LLM/TTS 级联，SEFA 目标 500-800ms，短回复为主

## Decision tree

> Each node is a named decision point an expert reaches in this field. Nodes have entry conditions,
> conditional options, judgment criteria, source citations, counterexamples, and outputs. A node
> without a source citation is not accepted. A branch without a counterexample is a fixed step in
> disguise.

### C1: 是否建立显式延迟预算

- entry_when: 开始搭管线或诊断"感觉慢"时
- options:
  - A: 不设预算，凭感觉优化最慢的组件 → leads_to: 回归无法定位
  - B: 按阶段分配预算，逐阶段监控回归 → leads_to: C2
- decide_by: 端到端间隙是各阶段之和；只优化一段只移动该段份额
- source: latency-budget-800ms.md
- counterexample: 非交互场景（旁白生成、批处理转写）不需要
- output: 阶段预算：endpointing 200–300 / ASR final 50–150 / LLM TTFT 200–400 /
  TTS 首 chunk 100–200 / 播放与 jitter 剩余；SEFA p50 <800ms、p95 <1500ms 参考线

### C2: 串行还是重叠

- entry_when: 预算分完后仍超支
- options:
  - A: 严格串行（等 final→LLM→整段→TTS→整段→播）→ leads_to: 付全部阶段之和
  - B: 重叠执行：partial 投机推理、首 token 即合成、首 chunk 即播 → leads_to: C3
- decide_by: 重叠后总延迟接近最长一段而非全部之和；这是预算里最大的可回收项
- source: latency-budget-800ms.md, streaming-everywhere.md
- counterexample: 投机成本高（推理贵、打断率极高、partial 抖动大）时收敛回串行
- output: ASR partial→prompt 预构造；LLM 流→chunker→TTS stream→播放器逐 chunk

### C3: 慢轮次兜底策略

- entry_when: 某些轮次合法地超预算（长推理、网络抖动）
- options:
  - A: 沉默等到结果 → leads_to: 死空气被记成"agent 卡了"
  - B: 阈值后发短 acknowledgment cue → leads_to: C4
- decide_by: 感知延迟可独立于实测延迟管理；cue 是编排策略不是性格脚本
- source: streaming-everywhere.md
- counterexample: 响应本来就在预算内时发 cue 反而画蛇添足
- output: LLM TTFT 超阈值（如 600ms）时派蒙式短 cue（"嗯……让我想想"），
  结果落地立刻接答案

### C4: 缓冲与传输粒度

- entry_when: 设计音频帧大小与播放 buffer
- options:
  - A: 大 buffer 求稳 → leads_to: 首音延迟与打断后残音都变大
  - B: 小帧 streaming + 播放 buffer 最小化 → leads_to: C5
- decide_by: buffer 深度同时是首音延迟项和 barge-in 残音项；本地链路允许小 buffer
- source: latency-budget-800ms.md（playback start 阶段）, barge-in-two-layers.md
- counterexample: 抖动网络（远程部署、弱网）需要更深 buffer 换稳定
- output: 小帧采集/播放，buffer 只保留"稳定播放所需最小值"

### C5: 指标纪律

- entry_when: 定义观测口径
- options:
  - A: 只看端到端 SEFA → leads_to: 回归不知哪段劣化
  - B: 逐轮记录各阶段时间戳，分阶段出 p50/p95 → leads_to: 完成
- decide_by: 预算是分阶段的，测量也必须分阶段
- source: latency-budget-800ms.md（Twilio 分阶段表、逐阶段回归定位）
- counterexample: 原型第一周可先只记 SEFA 总量跑通，再补分阶段
- output: doc 06 §3 九时间戳 + SEFA + barge-in latency 写入 data/ latency log

## Stance and rejected defaults

把延迟当预算管理：分阶段分配、重叠执行、逐轮记录、回归按阶段定位。拒绝：只盯端到端均值；
组件串行等待；用 ack cue 掩盖本来可以修掉的慢段；为"稳定"默认加深 buffer。

## Failure signals

- SEFA 达标但某阶段 p95 飘（均值掩盖长尾）；
- 优化单点后端到端无感（该点份额本来就小）；
- 打断后听到残音（播放 buffer 过深）；
- cue 在结果已就绪时还在发（阈值与状态脱节）。

## Relationships without merger

- turn-taking：endpointing 是本预算里最大也最可调的一段；投机执行是两边握手点。
- chinese-tts-eval / streaming-asr-zh：各 provider 的实测延迟进入本预算的对应阶段。
