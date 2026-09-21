# reference/ — 开源参考仓库

本目录以 git submodule（浅克隆 `--depth 1`）挂载项目依赖/候选的开源项目源码，
供开发时阅读参考。**不要把它们当依赖直接 import**——依赖走 pyproject 正常安装，
这里只是"离线的可读文档"。

克隆本仓库后初始化：

```bash
git submodule update --init --depth 1
```

更新到上游最新：

```bash
git submodule update --remote --depth 1
```

## 清单

| 目录 | 项目 | 本项目用途 | 阶段 |
|------|------|-----------|------|
| `pipecat/` | pipecat-ai/pipecat | 实时语音 pipeline 底盘；processor/transport/VAD/turn 机制实现细节 | Phase 1 核心依赖 |
| `smart-turn/` | pipecat-ai/smart-turn | 轮次判定模型（v3 ONNX）；判定逻辑、参数、训练数据来源 | Phase 1 核心依赖 |
| `silero-vad/` | snakers4/silero-vad | VAD；参数语义、窗口大小、阈值行为 | Phase 1 核心依赖 |
| `fish-speech/` | fishaudio/fish-speech | Fish Audio 对应开源实现；WS 协议与模型行为参考（我们用的是其云服务 API） | Phase 1 参考 |
| `funasr/` | modelscope/FunASR | 后续本地 ASR 候选（含 SenseVoice 生态入口） | 后续评估 |
| `cosyvoice/` | FunAudioLLM/CosyVoice | 后续自部署 TTS 候选 | 后续评估 |
| `livekit/` | livekit/livekit | 后续多端/WebRTC transport 候选；Phase 1 不强依赖 | Phase 2+ |

## 阅读优先级

第一阶段真正要读的只有前三个 + fish-speech 的协议部分。其余是为后续阶段备料，
现在不需要深入。
