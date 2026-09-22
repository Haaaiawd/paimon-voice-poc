"""TASK-004 人工验收入口：麦克风 → Silero VAD → Smart Turn，实时打印判定。

用法：

    python -m src.runtime.audio_demo

对麦克风说话即可看到：
- [VAD] speech_started / speech_stopped / 状态迁移（stop_secs=0.2）
- [TURN] complete / incomplete + 模型概率与耗时 + 来源（model / silence_fallback）

Ctrl+C 退出。本层不接 ASR/LLM/TTS，纯本地音频链路。
"""

from __future__ import annotations

import asyncio
import sys

from ..turn.smart_turn_adapter import SMART_TURN_PARAMS, SmartTurnAdapter
from ..turn.vad_adapter import VAD_PARAMS, SileroVADAdapter
from .mic import MicCapture

SAMPLE_RATE = 16000
BLOCKSIZE = 512  # 32ms @16kHz，Silero num_frames_required


async def _run() -> None:
    vad = SileroVADAdapter(sample_rate=SAMPLE_RATE)
    turn = SmartTurnAdapter(sample_rate=SAMPLE_RATE)
    await turn.setup()

    print(
        f"[cfg] vad.stop_secs={vad.params.stop_secs} "
        f"smart_turn(stop_secs={turn.params.stop_secs}, "
        f"pre_speech_ms={turn.params.pre_speech_ms}, "
        f"max_duration_secs={turn.params.max_duration_secs})"
    )
    assert vad.params.stop_secs == 0.2
    assert SMART_TURN_PARAMS.stop_secs == 1.2

    def _on_complete(v):
        p = f"{v.probability:.3f}" if v.probability is not None else "n/a"
        print(f"[TURN] >>> TURN COMPLETE (p={p}, source={v.source})")

    turn.on_turn_complete = _on_complete

    try:
        mic = MicCapture(sample_rate=SAMPLE_RATE, blocksize=BLOCKSIZE)
        mic.start()
    except Exception as e:
        print(f"[mic] open failed: {e}", file=sys.stderr)
        print("[mic] no input device? (该 demo 面向 Windows 本机麦克风)", file=sys.stderr)
        await turn.cleanup()
        await vad.cleanup()
        return

    print("[demo] listening — speak into the mic, Ctrl+C to quit")
    last_state = None
    try:
        async for pcm in mic.frames():
            res = await vad.analyze(pcm)
            if res.state != last_state:
                print(f"[VAD] {last_state} -> {res.state}")
                last_state = res.state
            if res.started:
                print("[VAD] speech_started")
                await turn.user_speech_started(vad.params.start_secs)
            if res.stopped:
                print("[VAD] speech_stopped")
                verdict = await turn.user_speech_stopped(vad.params.stop_secs)
                p = (
                    f"{verdict.probability:.3f}"
                    if verdict.probability is not None
                    else "n/a"
                )
                ms = (
                    f"{verdict.inference_ms:.0f}ms"
                    if verdict.inference_ms is not None
                    else "n/a"
                )
                tag = "complete" if verdict.complete else "incomplete"
                print(f"[TURN] {tag} (p={p}, {ms})")
            await turn.append_audio(pcm)
    except KeyboardInterrupt:
        pass
    finally:
        mic.stop()
        await turn.cleanup()
        await vad.cleanup()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
