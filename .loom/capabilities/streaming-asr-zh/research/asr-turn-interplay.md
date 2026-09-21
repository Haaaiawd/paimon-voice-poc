# ASR 断句与轮次判定的边界划分

Source: 项目 doc 02/03 设计 + Picovoice endpointing 分层 + DashScope 参数语义

本项目架构里有两套"结束判定"并存：ASR 服务端的断句（Paraformer VAD 断句/
max_sentence_silence）和客户端的 Smart Turn 轮次判定。专家做法是明确分工：
ASR 事件只当"转写流"用（partial/final 喂给 TurnManager 作语言层信号），
轮次权完全归 Smart Turn + Conversation Core——否则两套阈值会互相干扰，
出现"ASR 判句子结束但用户话没说完"时 pipeline 行为分裂。

工程含义：
- ASR adapter 输出统一为 ASR_PARTIAL / ASR_FINAL 事件，不直接驱动状态机迁移；
- max_sentence_silence 应调到偏短（如 300–600ms），让 final 尽快落地喂给 TurnManager，
  而不是让服务端帮我们等"用户说完了"；服务端等得久 = SEFA 里 ASR final 阶段膨胀；
- partial 结果本身就是投机资本：TurnManager 拿到 partial 可以提前构造 prompt，
  ASR final 到达时立刻发出（对应 Picovoice 的语言层信号 + 投机执行）。

失败信号：把 ASR sentence_end 直接接成 turn_complete（双重 endpointing）；
或为等 ASR final 而让 LLM 闲置——正确姿势是 partial 驱动准备、final 驱动提交。
