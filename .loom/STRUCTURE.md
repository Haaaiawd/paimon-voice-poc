# Project structure

> Where things live in this project. The Agent reads this before creating or moving files.
> Update this when the structure changes. Delete sections that do not apply. Add sections
> that do. This is a map, not a prescription — each project declares its own conventions.

以下按 `04_TECH_STACK_AND_OPEN_SOURCE.md` §10 约定布局；TASK-002 已落
`src/providers/{asr,llm,tts}/base.py` 抽象与 `llm/openai_compatible.py` adapter。
创建文件时遵循；与现状不符时以现状为准并更新本文件。

## Source code

```text
src/
  conversation/     # 核心资产：TurnManager / InterruptionManager / InitiativePolicy /
                    # ContextManager / 状态机（自研，不依赖供应商 SDK）
  character/        # 派蒙人格、Behavior Policy、情绪标签
  turn/             # vad_adapter.py、smart_turn_adapter.py
  providers/
    asr/            # ASRProvider 抽象 + 各云端实现
    llm/            # LLMProvider 抽象 + OpenAI-compatible 等
    tts/            # TTSProvider 抽象（stream_audio / cancel）+ 赛马实现
  metrics/          # latency log、SEFA、barge-in 计时
  runtime/          # 音频采集/播放、pipeline 组装、入口 main、
                    # ws_gateway.py（WS 契约投影层，FRONTEND_DEMO_DESIGN §4）
```

业务层不得直接 import 供应商 SDK，一律经 `providers/` 与 `turn/` 的 adapter。

## Frontend demo

```text
frontend/           # 独立前端 demo（Vite + React + TS + Blackchalk）
  src/
    components/     # ChatBubble / Composer / EmotionBadge / TypingIndicator / StateBar
    backend/        # ChatBackend 接口 + MockBackend / WsBackend 双实现
    mocks/          # 预设对话剧本（真 AgentReply 形状）
    styles/         # Blackchalk theme + 中文手写体回退链
  TODO.md           # 前端 demo 进度清单
  package.json
  vite.config.ts
```

前端与后端仅经 WebSocket JSON 契约通信，不共享代码；前端不引入 `.loom/`，进度由 `TODO.md` 管理。契约唯一事实源：`.loom/design/FRONTEND_DEMO_DESIGN.md` §4。

## Tests

`tests/` 镜像 `src/` 结构；`06_MVP_AND_EVALUATION.md` §4 的测试用例 A–G 是对话行为的
验收基准。赛马脚本放 `scripts/`（TTS/LLM benchmark）。

## Documents

根目录 `00_`–`07_*.md` 为项目设计文档（编号顺序即阅读顺序），`README.md` 为入口。
`.loom/` 为 LOOM 状态，纳入版本控制。

## Configuration and build

`pyproject.toml`（Python 3.12 与依赖清单）、`.env`（API keys，不入库）、`.env.example`、
`Dockerfile`（依赖打包用）。语音闭环跑 Windows 原生环境，不走 WSL。

## Assets and fixtures

`assets/`：测试音频样本、赛马用统一测试句集。`data/`：latency log 输出（gitignore）。

## Reference（submodule）

`reference/`：7 个开源项目的浅克隆 submodule（pipecat / smart-turn / silero-vad /
fish-speech / funasr / cosyvoice / livekit），只作阅读参考，不作为依赖 import。
清单与同步命令见 `reference/README.md`。

## Conventions

- Python，async pipeline（Pipecat processor/service 模式）。
- 事件命名沿用 `03_CONVERSATION_CORE.md` §4（USER_SPEECH_STARTED、TURN_COMPLETE 等）。
- 状态机状态名：IDLE / LISTENING / POSSIBLE_END / THINKING / SPEAKING / INTERRUPTED / SILENCED。
