# TASK-008：TTS 双 adapter 赛马（Fish Audio + 百炼 CosyVoice）

## Role
你是 Paimon Voice PoC 的编码代理。请严格按 LOOM task 定义实现，不扩范围，不改 00–07 文档。

## 目标
实现两个 TTS adapter，统一落到 `src/providers/tts/`，满足 `TTSProvider` 抽象；在真实管线内对两家做热 TTFA 赛马，产出可落盘的对比表与决策记录。

## 已就绪上下文
- `TASK-002` 完成：`ASRProvider/LLMProvider/TTSProvider` 抽象已落地，业务层零供应商 SDK import。
- `TASK-004` 完成：本地音频采集/播放/Silero VAD/Smart Turn 已实现，`StreamingPlayer` 支持 `stop()` 清 buffer。
- `TASK-007` 完成：DashScope ASR 真实端点已跑通，`.env` 中通常已有 `DASHSCOPE_API_KEY`。
- 项目使用 `sounddevice` 做音频 I/O，`httpx`/`websockets` 做网络，`msgpack` 可能用于二进制帧。
- 终端/宿主环境：Linux 可能无声卡，测试需覆盖无设备降级。

## 设计约束与验收口径
### 1. Fish Audio adapter（`src/providers/tts/fish_audio.py`）
- 接入 `s2.1-pro-free` 模型，走 Fish Audio WebSocket 流式接口。
- 实现 `TTSProvider`：`stream_audio(text, ...)` 产出 PCM 音频流；`cancel()` 立即停推、清本地 buffer、可立刻重开。
- 语义边界 flush：文本按语义切分后推送，避免等待整段才发。
- 测试：`tests/test_fish_tts.py` 覆盖流式写入、cancel 行为、错误透传；无 key 时优雅降级。

### 2. 百炼 CosyVoice adapter（`src/providers/tts/bailian_cosyvoice.py`）
- 接入 `cosyvoice-v3-flash`，走 DashScope 百炼 WebSocket 流式接口。
- 使用 `continue-task` 增量推送文本，`finish-task` 收尾；与 DashScope ASR 复用同一账号/endpoint 风格。
- 同样实现 `TTSProvider` 接口，cancel 行为同 Fish。
- 测试：`tests/test_bailian_tts.py` 覆盖 continue/finish 流程、cancel、无 key 降级。

### 3. 赛马与热 TTFA
- 同一测句集（建议 5–10 句中文，含长句/短句/标点），在真实管线内对两家各跑 ≥10 轮。
- 度量热 TTFA：WS 常驻连接下，从 `stream_audio(text)` 调用到首字节音频可播放的端到端时间。
- 输出 `data/tts_benchmark/bench_*.json`、`latest.json`、`latest.md`，含 mean/p50/p95 + 取消成功率 + 残音/异常备注。
- 胜出者写入 `.loom/design/decisions.md` 或更新现有决策记录；若任一 key 缺失，标注 `MISSING_KEY` 并保持 `TTSProvider` 抽象可插第二家。

## 边界
- 只写 `src/providers/tts/` 与对应测试/脚本，不改 `00_READ_ME_FIRST.md` 到 `07_DECISIONS_AND_OPEN_QUESTIONS.md`。
- 不接真实 ASR/LLM pipeline；赛马可在最小 harness 内完成。
- `cosyvoice-v3.5-flash` 当前账号 418 不可用，只用 `v3-flash`。
- 赛马结论不锁死接口；`TTSProvider` 抽象保持第二家随时可插。

## 交付物
1. `src/providers/tts/fish_audio.py`
2. `src/providers/tts/bailian_cosyvoice.py`
3. `tests/test_fish_tts.py`
4. `tests/test_bailian_tts.py`
5. `scripts/bench_tts.py`
6. `data/tts_benchmark/` 结果文件
7. 决策记录更新

## 完成通知
任务完成后，执行：
```bash
skillhome-notify tts success "TTS 双 adapter 实现与赛马完成" $(date +%s)
```
并将以下 footer 写入 `/home/haa/.skillhome/notifications/latest.json`：
```json
{
  "source": "assistant",
  "task": "tts dual adapter",
  "deliverables": [
    "src/providers/tts/fish_audio.py",
    "src/providers/tts/bailian_cosyvoice.py",
    "tests/test_fish_tts.py",
    "tests/test_bailian_tts.py",
    "scripts/bench_tts.py",
    "data/tts_benchmark results",
    "decision record update"
  ],
  "status": "success",
  "errors": "none"
}
```
