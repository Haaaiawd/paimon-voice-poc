# 产品范围与核心体验

- Kind: product
- Status: confirmed
- 规范文档：`01_PRODUCT_SCOPE.md`（本文件只列决策要点，细节以根文档为准）

## Responsibility in the whole

定义"做什么、不做什么、为什么"。防止范围漂移成通用语音 Agent 平台。

## Inputs, outputs, and boundaries

- 做：PC 本地实时语音 Companion，角色派蒙；低延迟 + 自然轮次 + 可打断 + 适度主动。
- 不做（第一阶段）：3D、X4 Air、直播、手机 App、视觉、长期记忆、自训练模型。
- 体验十条硬性要求见 `01_PRODUCT_SCOPE.md` §2.1（不抢话、可打断、打断后知道自己没说完等）。

## Components and control flow

问题优先级排序：Turn-taking > Barge-in > Latency > Persona > Initiative > Memory。
任何优化不得以牺牲轮次自然度换延迟。

## Data and state

无数据面；状态机归 conversation-core 设计文档。

## Interfaces and dependencies

载体：PC 本地，麦克风进、扬声器/耳机出。第一阶段用耳机规避 AEC。

## Failure, safety, and recovery

失败定义：闭环不自然（抢话/等待明显/打断后上下文错乱）——其余功能全部无意义。

## Implementation constraints

成功标准：连续聊 5–10 分钟仍自然（`01_PRODUCT_SCOPE.md` §5）。

## Verification strategy

由 mvp-evaluation 设计文档与 TASK-012 承接。

## Related documents and capabilities

`00_READ_ME_FIRST.md`、`07_DECISIONS_AND_OPEN_QUESTIONS.md`（D-001）。
