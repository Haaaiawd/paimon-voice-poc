# 中文 TTS 选型与流式评估

> One dossier covers one recognizable professional field. Keep UI/UX, visual art direction, game
> design, psychology, biology, security, and other fields separate when their evidence and judgments
> differ. Put cross-field synthesis in the affected design document, not in a hybrid capability title.
> The kernel of a capability is a decision tree with conditional branches that captures how an expert
> in this field thinks — not a fixed step list or a declarative stance document. It is a resource for
> the Agent to use, not a script it must follow. When evidence points outside the tree, trust the
> evidence and update the tree.

## Field identity and boundary

语音合成 provider 的工程评估与接入：流式协议选择、flush/缓冲语义、取消语义、
中文口语自然度与情绪控制的实测方法。不含端到端预算分配（low-latency-pipeline）、
人格文案（设计层）。

## Project scenario

双 TTS adapter 实测赛马：Fish s2.1-pro-free（免费但跨境延迟高）vs 百炼 cosyvoice-v3-flash（直连 ~0.6-0.8s + Instruct 情绪控制），真实管线数据裁决

## Decision tree

> Each node is a named decision point an expert reaches in this field. Nodes have entry conditions,
> conditional options, judgment criteria, source citations, counterexamples, and outputs. A node
> without a source citation is not accepted. A branch without a counterexample is a fixed step in
> disguise.

### C1: 接入协议选择

- entry_when: 设计 TTSProvider 的各家实现（Fish / 百炼 CosyVoice）
- options:
  - A: HTTP streaming（tts.stream）→ leads_to: 适合已有完整文本
  - B: WebSocket（Fish stream_websocket / v1/tts/live；百炼 continue-task 增量文本）
    → leads_to: C2
- decide_by: 本项目文本来自 LLM token 流，句子未完成就要开口——只有 WS 模式匹配；
  两家都提供 WS 双向流式，协议细节不同但语义同构（增量文本→音频流→收尾事件）；
  HTTP 留给"固定短句"（如 acknowledgment cue）
- source: fish-audio-streaming.md, bailian-cosyvoice-streaming.md
- counterexample: 全部回复都是预生成固定文本时 HTTP 更简单且 TTFA 更低
- output: 双 adapter 主路均为 WebSocket 常驻连接；cue 类固定短语允许走 HTTP 捷径

### C2: 文本推送与 flush 时机

- entry_when: LLM token 流经 Text Chunker 进入 TTS
- options:
  - A: 逐 token 直推不 flush → leads_to: 服务端持续等上下文，TTFA 膨胀
  - B: 语义边界（句尾/轮次末）发 FlushEvent → leads_to: C3
- decide_by: Fish 服务端为自然度缓冲文本，需在语义边界发 FlushEvent；
  百炼端用 continue-task 分句、finish-task 强制收尾——语义同构，
  adapter 统一暴露"按语义边界推送文本块"的契约，各自映射到本家协议
- source: fish-audio-streaming.md, bailian-cosyvoice-streaming.md
- counterexample: 回复极短（单句）时 flush 时机无争议，直接 commit
- output: Text Chunker 变薄：按标点/语义边界聚合；Fish 边界发 flush + latency=balanced，
  百炼按 continue-task 分句节奏推送

### C3: 取消语义实测

- entry_when: 实现 TTSProvider.cancel()
- options:
  - A: 假设 cancel 后服务端停推 → leads_to: barge-in 残音风险
  - B: 实测取消后行为：服务端是否推已生成音频、本地 buffer 谁清、重开一句的状态残留
    → leads_to: C4
- decide_by: cancel 是 barge-in 硬依赖，不能假设；用例 G（连续互怼）会密集触发
  取消后立刻重开
- source: tts-eval-methodology.md
- counterexample: 无打断功能的播报型应用可以不测
- output: adapter 层 cancel 测试（停推断言 + 本地 buffer 清理 + 连续 cancel/start）

### C4: 评估方法与测句集

- entry_when: 判断 Fish 是否够用 / 后续赛马
- options:
  - A: 官网 demo / 通用测句 → leads_to: 与本项目域不匹配
  - B: 派蒙域测句集（短句吐槽、语气词、反问句占大头）+ 真实管线 TTFA → leads_to: C5
- decide_by: doc 06 纪律：同一组句、真实对话体验决定、不看官网 benchmark
- source: tts-eval-methodology.md
- counterexample: 做通用语音助手（长句播报为主）才需要长句权重大
- output: scripts/ 下测句集 + TTFA/取消/情绪实测记录表

### C5: 情绪标签映射

- entry_when: LLM 结构化输出的 emotion 字段要驱动 TTS
- options:
  - A: 假设 provider 情绪参数直接可用 → leads_to: 标签被静默忽略的风险
  - B: 逐个标定 emotion → provider 参数映射；不支持时靠 prompt 让文本自带情绪
    → leads_to: 完成
- decide_by: 各家情绪控制能力差异大，映射表要实测标定而非假设；百炼 v3 音色有
  Instruct 通道（"你说话的情感是happy。" 文本指令），Fish 侧暂无等价物，
  只能靠 prompt 措辞带情绪——这是赛马裁决的维度之一
- source: tts-eval-methodology.md, bailian-cosyvoice-streaming.md
- counterexample: 单层情绪（全程一个语气）产品不需要映射层
- output: emotion → 各家映射表（百炼：Instruct 情感值；Fish：prompt 措辞降级），
  缺失标签降级为 neutral

## Stance and rejected defaults

以真实管线实测为准：WS 流式接入、语义边界 flush、cancel 必须实测断言、测句集贴合
派蒙域、情绪映射逐个标定。拒绝：用官网 benchmark 选型；假设 cancel 语义；
为"最自然"攒长文本牺牲 TTFA；让情绪标签静默失效。

## Failure signals

- 单句 demo 好听但连续对话延迟漂移；
- cancel 后偶发残音或下一句开头串音；
- emotion 标签发出去了但语气无变化（被静默忽略）；
- TTFA 在 API 直连测试很好、在真实管线里翻倍（缓冲点找错）。

## Relationships without merger

- low-latency-pipeline：本 dossier 的 TTFA 是 SEFA 预算的最后一个大项；
  flush 策略是"自然度 vs 首包延迟"的本地权衡。
- turn-taking：cancel 语义是 barge-in 媒体层正确性的下游依赖。
- streaming-asr-zh：Realtime TTS v3 的 provider-neutral 事件面（fish/minimax 同协议）
  说明 adapter 抽象的行业惯例方向，与 ASR 侧抽象互证。
