# 系统架构：模块流水线与状态机

- Kind: system
- Status: confirmed
- 规范文档：`02_SYSTEM_ARCHITECTURE.md`

## Responsibility in the whole

定义音频流水线各模块职责、控制链（InterruptionManager / LatencyMetrics）与对话状态机。

## Inputs, outputs, and boundaries

```text
Microphone → Audio Capture → VAD → Turn Detector → Streaming ASR
→ Conversation Core → LLM → Text Chunker → Streaming TTS → Audio Playback
```

旁路控制链：InterruptionManager、Latency Metrics。LLM 不判断轮次，不碰底层音频状态。

## Components and control flow

状态机最小集：IDLE / LISTENING / POSSIBLE_END / THINKING / SPEAKING / INTERRUPTED / SILENCED。
典型路径与迁移条件见 `02_SYSTEM_ARCHITECTURE.md` §3。

## Data and state

每轮延迟时间戳（`06` §3）：t_user_speech_end … t_playback_stopped。SEFA 目标 500–800ms 区间。

## Interfaces and dependencies

```python
class ASRProvider:  async def stream(...)
class LLMProvider:  async def stream_reply(...)
class TTSProvider:  async def stream_audio(...) / async def cancel(...)
```

业务层不直接 import 供应商 SDK；全部经 `src/providers/` 与 `src/turn/` adapter。

## Failure, safety, and recovery

播放器必须支持立即 stop、清空未播 buffer、报告实际播放到哪里——打断正确性依赖它。

## Implementation constraints

不把一切塞进 LLM；VAD/Turn/ASR/TTS 各由专业模块负责（`02` §4）。

Capability 决策树给出的初始参数（实测后可调，调整走 `loom decision`）：
- Silero VAD `stop_secs=0.2`（停顿事件早产生，判定交给模型）；Smart Turn
  `SmartTurnParams.stop_secs=1.2`（中文 incomplete 误判的实时优先兜底，D-2026-09-22-6thz）、`pre_speech_ms=500`、
  `max_duration_secs=8`（turn-taking C2/C3）；
- Paraformer `semantic_punctuation_enabled=false`、`max_sentence_silence≈500ms`、
  `disfluency_removal_enabled=false`（streaming-asr-zh C1/C2/C4）；
- Fish TTS：WebSocket 主路、语义边界发 flush、`latency="balanced"`（chinese-tts-eval C1/C2）；
- barge-in：SPEAKING 中 VAD speech_started 立即停播+取消在途（turn-taking C4）；
- 阶段延迟预算：endpointing 200–300 / ASR final 50–150 / LLM TTFT 200–400 /
  TTS 首 chunk 100–200ms（low-latency-pipeline C1）。

## Verification strategy

TASK-004（音频与轮次层）、TASK-010（端到端组装）验收。

## Related documents and capabilities

`03_CONVERSATION_CORE.md`、D-002/D-003。
