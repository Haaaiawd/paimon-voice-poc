# 实时语音轮次检测与打断工程

> One dossier covers one recognizable professional field. Keep UI/UX, visual art direction, game
> design, psychology, biology, security, and other fields separate when their evidence and judgments
> differ. Put cross-field synthesis in the affected design document, not in a hybrid capability title.
> The kernel of a capability is a decision tree with conditional branches that captures how an expert
> in this field thinks — not a fixed step list or a declarative stance document. It is a resource for
> the Agent to use, not a script it must follow. When evidence points outside the tree, trust the
> evidence and update the tree.

## Field identity and boundary

语音交互的 endpointing/turn-taking 工程：判断用户何时说完、agent 何时可说、打断如何触发
与恢复。覆盖 VAD 参数、turn 模型、投机执行、barge-in 两层处理。不含 ASR 服务选型
（streaming-asr-zh）、延迟预算分配（low-latency-pipeline）、人格主动性策略（属设计层）。

## Project scenario

中文实时自然对话：VAD 0.2s + Smart Turn 模型判停；模型误判 incomplete 时 1.2s 实时优先兜底，记录 fallback/抢话率并可回调 1.8s；用户可立即打断派蒙，派蒙暂不反向打断用户

## Decision tree

> Each node is a named decision point an expert reaches in this field. Nodes have entry conditions,
> conditional options, judgment criteria, source citations, counterexamples, and outputs. A node
> without a source citation is not accepted. A branch without a counterexample is a fixed step in
> disguise.

### C1: 轮次结束判定策略

- entry_when: 需要决定"用户说完了吗"的机制
- options:
  - A: 固定静音超时（SpeechTimeout）→ leads_to: 放弃，见 counterexample
  - B: Smart Turn 模型判定 + 静音兜底 → leads_to: C2
  - C: LLM 判语义完整（FilterIncomplete）→ leads_to: 作为 B 的补充而非替代（多一次 LLM 往返）
- decide_by: 中文口语句中停顿/拖长尾音频繁，固定超时双向失败；本地 CPU 可跑 Smart Turn v3
  （<100ms），模型判定是自然度上限
- source: pipecat-smart-turn-mechanics.md, picovoice-endpointing-tiers.md
- counterexample: 命令式单唤醒词交互（"小爱同学关灯"）或极端延迟敏感且语句模式固定的场景，
  固定超时反而够用且零模型成本
- output: TurnAnalyzerUserTurnStopStrategy + LocalSmartTurnAnalyzerV3 为默认 stop strategy

### C2: VAD 与 Smart Turn 的参数分工

- entry_when: Smart Turn 启用后配置 VAD
- options:
  - A: VAD stop_secs 设长（0.6s+）→ leads_to: 模型介入太晚，停顿判定滞后
  - B: VAD stop_secs 设短（0.2s，官方推荐）→ leads_to: C3
- decide_by: Smart Turn 需要在每次真实停顿时尽早拿到分析机会；VAD 短 stop_secs 让
  "停顿事件"早产生，判定质量交给模型
- source: pipecat-smart-turn-mechanics.md
- counterexample: 纯 VAD endpointing（无模型）时 stop_secs 必须长，否则每个微停顿都判轮次结束
- output: Silero VAD stop_secs=0.2；SmartTurnParams.stop_secs 独立控制兜底

### C3: incomplete 判定后的兜底

- entry_when: Smart Turn 判 incomplete 但用户迟迟不续说
- options:
  - A: 无限等待 → leads_to: 死等，违反延迟预算
  - B: SmartTurnParams.stop_secs=1.2s 静音超时兜底 complete → leads_to: C4
- decide_by: 防止思考停顿/拖长尾音导致死锁；Smart Turn 中文准确率 85.79%/FNR 9.26%
  （23 语言倒数第三），实测完整中文句会误判 incomplete。用户明确选择实时优先，将默认 3.0s
  收紧为 1.2s；metrics 继续记录 model vs fallback 来源，按真实抢话率决定是否回调到 1.8s。
- source: pipecat-smart-turn-mechanics.md, smart-turn-zh-benchmark.md
- counterexample: 用户场景含长思考（读卡号、查信息）时主动放宽该值
- output: stop_secs=1.2，列为可调参数；metrics 记录判定来源、抢话率与 fallback 率

### C4: barge-in 触发条件

- entry_when: SPEAKING 状态下需要判定"用户是否在打断"
- options:
  - A: 任何 VAD speech_started 立即触发 → leads_to: 快但可能被 backchannel（"嗯""哦"）误伤
  - B: 要求最短语音时长或 ASR partial 确认再触发 → leads_to: 准但增加打断延迟
  - C: 媒体层立即停 + 逻辑层取消，backchannel 容忍留作后续增强 → leads_to: C5
- decide_by: 打断的第一优先级是"快停"（媒体层立即 flush），误判代价第一版可接受
  （耳机场景无自回声）；精细 backchannel 区分等真实数据
- source: barge-in-two-layers.md
- counterexample: 扬声器外放或嘈杂环境（误报多）时必须加最短时长/语义确认，否则体验碎裂
- output: VAD speech_started → InterruptionManager 六步立即执行；最短打断时长参数留 hook

### C5: 打断后的上下文处置

- entry_when: 播放被中断，已生成内容部分播出
- options:
  - A: 整句写入历史或整句丢弃 → leads_to: 模型误以为用户听到了没听到的内容
  - B: heard history 与 logical history 分离，部分播出记为 interrupted turn → leads_to: C6
- decide_by: doc 03 §3 的结构：assistant_heard / generated_but_not_heard / event；推理层必须
  知道用户实际听到了什么
- source: barge-in-two-layers.md
- counterexample: 单轮问答型 agent（无连续上下文）可直接丢弃
- output: ContextManager 双历史 + interruption context payload

### C6: 投机执行边界

- entry_when: ASR partial 已到手但轮次未确认
- options:
  - A: 等 turn_complete 再启动 LLM → leads_to: SEFA 全额付 LLM TTFT
  - B: partial 驱动 prompt 预构造/投机推理，final/turn_complete 到达即提交 → leads_to: 完成
- decide_by: LLM TTFT 是预算最大单项之一；partial 已能让 TurnManager 做语言层判断
- source: picovoice-endpointing-tiers.md, asr-turn-interplay.md
- counterexample: partial 抖动剧烈（连续改口）导致投机成本高，或打断率极高时收敛为 A
- output: prompt 预构造管线；投机结果可静默丢弃

## Stance and rejected defaults

本项目用模型化 endpointing（Smart Turn v3）+ 短 VAD 停顿 + 静音兜底 + 两层 barge-in +
双历史 + 有限投机。拒绝：固定静音超时做轮次判定；ASR 服务端断句直接当轮次结束；
打断后把完整生成句写进历史；为省 CPU 退回纯 VAD endpointing（除非实测模型不可行）。

## Failure signals

- 用户句中停顿被抢答（endpointing 太激进 / VAD stop_secs 误当轮次超时用）；
- 用户讲完后死等（模型 incomplete + 兜底过长）；
- 打断后冒出"陈旧响应"（逻辑层取消不完整，签名式失败）；
- 用户"嗯"一声派蒙就被切断（backchannel 误伤，需要最短时长）；
- 派蒙打断自己（回声被 VAD 检成用户语音——第一版耳机规避）；
- fallback 率异常高（>10% 轮次靠超时兜底——中文模型短板的信号，不是接线问题）；
- 调试中无法说出一次 complete 是模型判的还是兜底判的（观测缺失）。

## Relationships without merger

- low-latency-pipeline：endpointing 占 SEFA 预算最大份额之一，本 dossier 决定"何时判定"，
  那个 dossier 决定"预算怎么分"；投机执行是两域的握手点。
- streaming-asr-zh：ASR 断句事件只是本 dossier 的输入信号之一，轮次权在本侧。
