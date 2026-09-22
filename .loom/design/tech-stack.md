# 技术选型与开源组件

- Kind: research
- Status: confirmed
- 规范文档：`04_TECH_STACK_AND_OPEN_SOURCE.md`

## Responsibility in the whole

决定哪些造轮子、哪些用成熟组件、各层 Provider 选型与赛马方法。

## Inputs, outputs, and boundaries

原则："站在开源项目上写自己的核心"，不 fork 魔改。自研范围 =
conversation/ + character/ + runtime coordination + metrics。

## Components and control flow

| 层 | 选型 |
|----|------|
| Runtime | Python 3.12 + Pipecat（自定义逻辑走独立 processor/service，不改其源码） |
| VAD | Silero VAD（只判 speech/non-speech） |
| Turn Detection | Pipecat Smart Turn（不自训练） |
| ASR | 阿里云百炼/通义听悟流式（DashScope；D-010 取代"暂不锁定"），key 已到位并实测通过 |
| LLM | OpenAI-compatible 抽象；默认百炼 `qwen-flash`（2026-09-22 同负载实测 TTFT mean 609ms、结构化 2/2；`qwen-turbo` 1678ms，3.7/3.8 flash 更慢），TTFT > TPS；模型仍可用 `QWEN_MODEL` 替换 |
| TTS | 百炼 cosyvoice-v3-flash 为主力（D-014 赛马裁决，热 TTFA p50 976ms vs Fish 1090ms，取消均 100%）；Fish s2.1-pro-free adapter 保留为第二家可插路径；CosyVoice 本地自部署留后续 |
| Transport | 第一版本地，不写死；LiveKit 留给后续多端 |

## Data and state

赛马测量数据（TTFT/TTFA 等）写入 `data/` 或 benchmark 输出，供选型决策用。

## Interfaces and dependencies

每个依赖经 adapter 使用：`turn/`（vad/smart_turn adapter）、`providers/asr|llm|tts/`。

## Failure, safety, and recovery

供应商故障可替换：接口抽象保证换 provider 不动 Conversation Core。

## Implementation constraints

暂不自己造：VAD / Turn Detection / ASR / TTS 模型、WebRTC 协议栈。
开发环境：Windows 原生 Python 3.12（音频闭环不在 WSL）；Docker 用于依赖打包（D-011）。

## Verification strategy

TASK-003 赛马脚本产出真实数据；不看官网 benchmark。

## Related documents and capabilities

`07_DECISIONS_AND_OPEN_QUESTIONS.md`、DECISIONS.md 全部条目。
