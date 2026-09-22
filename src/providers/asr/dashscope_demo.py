"""TASK-007 人工验收入口：麦克风 → DashScopeASR，实时打印 partial/final + 延迟。

用法：

    python -m src.providers.asr.dashscope_demo              # 麦克风（16kHz mono int16）
    python -m src.providers.asr.dashscope_demo --file x.pcm # 无麦环境：喂裸 PCM 文件
    python -m src.providers.asr.dashscope_demo --file x.wav # 或 wav（自动读参数）

对麦克风说中文即可看到：
- [+XXXXms] PARTIAL …（不稳定中间结果，随说话滚动修正）
- [+XXXXms] FINAL …（服务端 VAD 断句落地，max_sentence_silence=500ms）
- 结束时打印 first-event latency（首帧音频发出 → 首个识别事件）。

无输入设备时（无麦/远程）用 --file 模式，按真实节奏（32ms/帧）推流。
Ctrl+C 退出。本 demo 只验证 adapter，不接 Conversation Core（任务边界）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import wave
from collections.abc import AsyncIterator
from pathlib import Path

from dotenv import dotenv_values

from .dashscope import DEFAULT_PARAMETERS, DashScopeASR

SAMPLE_RATE = 16000
BLOCKSIZE = 512  # 32ms @16kHz，与 MicCapture 帧约定一致
FRAME_BYTES = BLOCKSIZE * 2  # int16 mono
ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


async def _file_frames(path: Path) -> AsyncIterator[bytes]:
    """按真实节奏（32ms/帧）推送文件中的 16kHz mono int16 PCM。"""
    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                raise SystemExit(
                    f"[file] need mono int16 wav, got "
                    f"ch={wf.getnchannels()} width={wf.getsampwidth()}"
                )
            rate = wf.getframerate()
            data = wf.readframes(wf.getnframes())
    else:
        rate = SAMPLE_RATE
        data = path.read_bytes()
    if rate != SAMPLE_RATE:
        raise SystemExit(f"[file] need {SAMPLE_RATE}Hz, got {rate}Hz")
    frame_secs = FRAME_BYTES / 2 / rate
    for off in range(0, len(data) - FRAME_BYTES + 1, FRAME_BYTES):
        yield data[off : off + FRAME_BYTES]
        await asyncio.sleep(frame_secs)


def _load_api_key() -> str:
    env = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
    key = (
        os.environ.get("DASHSCOPE_API_KEY") or env.get("DASHSCOPE_API_KEY") or ""
    ).strip()
    if not key:
        raise SystemExit(
            "[env] DASHSCOPE_API_KEY 为空——在 .env 填入百炼 key 或导出同名环境变量"
        )
    return key


async def _run(args: argparse.Namespace) -> None:
    asr = DashScopeASR(api_key=_load_api_key())
    p = asr.parameters
    print(
        f"[cfg] model={asr.model} "
        f"semantic_punctuation={p['semantic_punctuation_enabled']} "
        f"max_sentence_silence={p['max_sentence_silence']}ms "
        f"disfluency_removal={p['disfluency_removal_enabled']} "
        f"heartbeat={p['heartbeat']} proxy=None(direct)"
    )
    assert p["semantic_punctuation_enabled"] is False  # C1
    assert p["max_sentence_silence"] == 500  # C2
    assert DEFAULT_PARAMETERS["disfluency_removal_enabled"] is False  # C4

    mic = None
    if args.file:
        audio: AsyncIterator[bytes] = _file_frames(args.file)
        source = f"file:{args.file}"
    else:
        from ...runtime.mic import MicCapture

        try:
            mic = MicCapture(sample_rate=SAMPLE_RATE, blocksize=BLOCKSIZE)
            mic.start()
        except Exception as e:
            print(f"[mic] open failed: {e}", file=sys.stderr)
            print(
                "[mic] no input device? 用 --file 喂音频文件验证",
                file=sys.stderr,
            )
            await asr.close()
            return
        audio = mic.frames()
        source = "mic"
    print(f"[demo] source={source} — speak Chinese, Ctrl+C to quit")

    t0 = time.monotonic()
    n_partial = n_final = 0
    try:
        async for ev in asr.stream(audio, sample_rate=SAMPLE_RATE):
            ms = (time.monotonic() - t0) * 1000
            print(f"[+{ms:7.1f}ms] {ev.kind.upper():7} {ev.text}")
            if ev.kind == "partial":
                n_partial += 1
            else:
                n_final += 1
    except KeyboardInterrupt:
        pass
    finally:
        if mic is not None:
            mic.stop()
        await asr.close()

    if asr.first_event_latency_ms is not None:
        print(f"[metrics] first-event latency: {asr.first_event_latency_ms:.0f}ms")
    print(f"[metrics] events: {n_partial} partial / {n_final} final")
    if n_final == 0 and n_partial == 0:
        print(
            "[metrics] no ASR events — 检查 key / 网络 / 输入是否有声",
            file=sys.stderr,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file",
        type=Path,
        default=None,
        help="裸 PCM 或 wav（16kHz mono int16）；缺省用麦克风",
    )
    args = parser.parse_args()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
