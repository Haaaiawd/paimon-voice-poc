# Endpointing 分层信号与投机执行

Source: Picovoice Voice UX guide ch.3 "Latency, Turn-Taking, and Barge-In"

Endpointing 是系统里杠杆最高的时序决策：太早抢话、太晚全是死空气。朴素固定静音超时双向失败
——人会在句中停顿思考，超时设短就切断用户，设长则每一轮都附加整个超时。

生产级 endpointing 叠三层信号：
- 声学：VAD 提供"停顿开始、持续多久"的原始信号；
- 语言：流式 transcript 判断话语是否语义完整（"我想付我的" vs "我想付我的账单"）；
- 行为：分层响应——中置信度时**投机地开始推理但不发声**，高置信度才 commit；
  用户若续说，静默丢弃投机结果。

投机层是让 agent 既耐心又快的关键：容忍长停顿不插嘴，但真结束时响应已在准备。
VAD 的每个误报/漏报都会向下游复合：误报延迟 endpointing，漏报截断用户轮次。
