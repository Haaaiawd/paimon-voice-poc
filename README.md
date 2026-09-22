# paimon-voice-poc

PC 本地的实时中文语音 Companion 原型——角色是**派蒙**。

不是语音问答助手：目标是对话节奏——说完自动接话、随时可打断、
被打断后上下文不乱、偶尔主动开口、人格稳定。

## 当前状态（2026-09-22）

端到端语音闭环已跑通：

```text
浏览器麦克风 → PCM 16k 上行 → VAD → Smart Turn → Paraformer ASR
  → Conversation Core → qwen-flash (streaming) → CosyVoice (streaming)
  → WS audio.chunk + PCM 下行 → 浏览器首块即播
```

- 判停：VAD 0.2s + Smart Turn 模型判定，中文误判兜底 **1.2s**（实测 ~0.85s）
- LLM：默认 `qwen-flash`（同负载实测 TTFT ~609ms），`QWEN_MODEL` 可换
- TTS：默认 `cosyvoice-v3-flash` / `longhuhu_v3`（天真烂漫女童），`TTS_PROVIDER`/`TTS_VOICE` 可换
- 打断：全双工，用户开口即停播（实测 0.1–0.3ms）
- 前端：Vite + React + Blackchalk 手绘风 demo，文字/语音双模

已知限制：电脑扬声器外放会造成回声自打断（物理回灌），测试建议戴耳机；
端到端首音目前 ~2s 量级，距 500–800ms 目标还有距离。

## 快速开始

需要 Python ≥3.12 和 Node ≥20。

```bash
pip install -e ".[dev]"
cp .env.example .env        # 填 DASHSCOPE_API_KEY；其余可选
```

Windows 一键启动（后端 8766 + 前端 5173）：

```bat
start.bat
```

或手动两个终端：

```bash
python -m runtime.ws_gateway --port 8766        # 后端 WS gateway

cd frontend && npm ci
VITE_BACKEND=ws VITE_WS_URL=ws://127.0.0.1:8766/ws/chat npm run dev
```

`.env` 不入库。本机走代理时给 `*.aliyuncs.com` / `api.fish.audio`
配 DIRECT，否则每次连接 +~1.9s TLS 开销（详见 `.env.example`）。

也有无浏览器的终端入口：`python -m runtime.main --help`
（支持 mic / PCM 文件 / mock 文本输入）。

## 测试

```bash
python -m pytest -q                 # 后端全量（196+ 用例）
cd frontend
npm run typecheck && npm run build  # 前端
```

赛马/验收脚本在 `scripts/`：`bench_llm.py`、`bench_tts.py`、`mvp_acceptance.py`。

## 仓库布局

```text
00_–07_*.md     规范文档本体（编号即阅读顺序，从 00_READ_ME_FIRST 开始）
src/
  conversation/   核心资产：轮次/打断/主动性/上下文/状态机（自研）
  providers/      asr | llm | tts 抽象 + 各供应商 adapter
  turn/           Silero VAD、Smart Turn adapter
  character/      派蒙人格、情绪标签、prompt
  runtime/        pipeline 组装、main（终端入口）、ws_gateway（前端 WS 契约层）
  metrics/        latency log、SEFA
frontend/         浏览器 demo（契约见 .loom/design/FRONTEND_DEMO_DESIGN.md §4）
scripts/          benchmark 与 MVP 验收脚本
tests/            镜像 src/；用例 A–G 对应 06 §4
.loom/            LOOM 项目状态：PROJECT/STRUCTURE/DECISIONS、design/、
                  capabilities/、tasks.json
reference/        7 个开源项目浅克隆 submodule，只读参考不 import
data/             latency log 与测试产物（gitignore）
```

设计文档从 `00_READ_ME_FIRST.md` 开始按编号读；
项目事实与决策史看 `.loom/PROJECT.md` 和 `.loom/DECISIONS.md`。

## 协作约定

- 业务层不直接 import 供应商 SDK，一律经 `providers/`/`turn/` adapter
- Conversation Core 是核心资产，不外包给框架/供应商
- Agent 接手前先跑 `loom context`（见 `AGENTS.md`）
