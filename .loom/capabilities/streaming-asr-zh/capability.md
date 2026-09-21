# 中文流式 ASR 集成（DashScope）

> One dossier covers one recognizable professional field. Keep UI/UX, visual art direction, game
> design, psychology, biology, security, and other fields separate when their evidence and judgments
> differ. Put cross-field synthesis in the affected design document, not in a hybrid capability title.
> The kernel of a capability is a decision tree with conditional branches that captures how an expert
> in this field thinks — not a fixed step list or a declarative stance document. It is a resource for
> the Agent to use, not a script it must follow. When evidence points outside the tree, trust the
> evidence and update the tree.

## Field identity and boundary

中文流式语音识别服务接入：DashScope/Paraformer 实时协议、断句参数语义、
partial/final 事件如何服务于轮次判定与延迟预算。不含轮次判定本身（turn-taking）、
TTS 侧（chinese-tts-eval）。

## Project scenario

阿里云百炼 paraformer-realtime-v2 WebSocket，中文口语，ASR 只做信号不做轮次裁决，key 申请中

## Decision tree

> Each node is a named decision point an expert reaches in this field. Nodes have entry conditions,
> conditional options, judgment criteria, source citations, counterexamples, and outputs. A node
> without a source citation is not accepted. A branch without a counterexample is a fixed step in
> disguise.

### C1: 服务端断句模式

- entry_when: 配置 paraformer-realtime-v2 的断句参数
- options:
  - A: semantic_punctuation_enabled=true（语义断句）→ leads_to: 准但慢，适合转写不适合交互
  - B: =false（VAD 断句，默认）→ leads_to: C2
- decide_by: 交互场景延迟优先；官方自己也定位 VAD 断句为低延迟交互档
- source: dashscope-paraformer-realtime.md
- counterexample: 会议转写/长独白记录场景应开语义断句
- output: semantic_punctuation_enabled=false

### C2: max_sentence_silence 取值

- entry_when: VAD 断句模式下选静音阈值（默认 1300ms，范围 200–6000）
- options:
  - A: 保持默认 1300ms → leads_to: final 落地慢，SEFA 的 ASR 段膨胀
  - B: 调短（初始 ~500ms）→ leads_to: C3
- decide_by: 服务端断句只决定 ASR_FINAL 事件何时产生；轮次权在 Smart Turn——
  让服务端等得越久，语言层信号越晚到
- source: asr-turn-interplay.md, dashscope-paraformer-realtime.md
- counterexample: 若架构退回用服务端断句当轮次信号（无 Smart Turn），则要调长避免切碎
- output: max_sentence_silence 初始 500ms，进 metrics 观察后调

### C3: ASR 断句与轮次判定的权责

- entry_when: 设计 ASR 事件到状态机的接线
- options:
  - A: result-generated 的句子边界直接当 turn_complete → leads_to: 双重 endpointing 互相干扰
  - B: ASR 只产出 ASR_PARTIAL/ASR_FINAL 事件喂 TurnManager，轮次归 Smart Turn → leads_to: C4
- decide_by: 两套阈值并存时必须单一裁决者，否则"ASR 判句完但用户没说完"时行为分裂
- source: asr-turn-interplay.md
- counterexample: 无 Smart Turn 的极简 pipeline 可以直接用服务端断句（裁决者仍唯一）
- output: ASR adapter 事件面只含 partial/final，不接状态机迁移

### C4: 语气词处理

- entry_when: 配置 disfluency_removal_enabled
- options:
  - A: true（过滤"嗯/那个"）→ leads_to: LLM 输入更干净但丢犹豫信号
  - B: false（保留）→ leads_to: 完成
- decide_by: 语气词本身是轮次未完成的线索（"我觉得吧……嗯……"），且符合真实口语；
  本项目 TurnManager 要看语言层完整性
- source: asr-turn-interplay.md, dashscope-paraformer-realtime.md
- counterexample: 正式纪要/笔记场景过滤更专业
- output: disfluency_removal_enabled=false 起步；若实测噪音词污染严重再翻

## Stance and rejected defaults

ASR 是信号源不是裁决者：VAD 断句 + 短静音阈值让事件尽快落地，轮次权全部归
Conversation Core。拒绝：服务端 sentence_end 直驱状态机；为文本干净牺牲犹豫信号；
在 key 未到位前阻塞无关任务（adapter 可用 fake ASR 先跑通 pipeline）。

## Failure signals

- 用户句中停顿后 ASR final 迟迟不来（max_sentence_silence 过长）；
- ASR 断句与 Smart Turn 判定打架（抢话或死等的分裂行为）；
- 文本过净导致模型把犹豫听成肯定（disfluency_removal 误开）；
- 静音期连接掉线（heartbeat 未开或网络问题）。

## Relationships without merger

- turn-taking：本 dossier 提供 partial/final 信号与断句参数，轮次裁决在彼侧；
  "信号不裁决"是两个 dossier 的分界线。
- low-latency-pipeline：max_sentence_silence 与 partial 投机直接影响 SEFA 的 ASR 段。
