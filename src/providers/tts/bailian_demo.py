"""TASK-008 人工验收入口：BailianCosyVoiceTTS 真实合成 → StreamingPlayer 播放。

用法：

    python -m src.providers.tts.bailian_demo            # 合成内置测句并播放
    python -m src.providers.tts.bailian_demo --cancel   # 播到一半 cancel 再重开
    python -m src.providers.tts.bailian_demo --out x.pcm  # 无声卡：落盘裸 PCM

每句打印 TTFA（stream_audio 调用 → 首字节音频）与总时长；
WS 常驻连接跨句复用，第二句起即为热 TTFA。
无输出设备时自动降级为只统计（或用 --out 落盘）。
本 demo 只验证 adapter，不接 Conversation Core（任务边界）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

from dotenv import dotenv_values

from .bailian_cosyvoice import BailianCosyVoiceTTS

ENV_FILE = Path(__file__).resolve().parents[3] / ".env"

DEMO_CHUNKS: tuple[str, ...] = (
    "诶嘿，旅行者！",
    "你猜我刚才看到了什么？",
    "是甜甜花酿鸡哦，快给我留一口！",
)


def _load_api_key() -> str:
    env = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
    import os

    key = (
        os.environ.get("DASHSCOPE_API_KEY") or env.get("DASHSCOPE_API_KEY") or ""
    ).strip()
    if not key:
        raise SystemExit(
            "[env] DASHSCOPE_API_KEY 为空——在 .env 填入百炼 key 或导出同名环境变量"
        )
    return key


async def _chunks(texts: tuple[str, ...], delay: float = 0.15) -> AsyncIterator[str]:
    """模拟 LLM token 流经 Text Chunker 的语义边界节奏。"""
    for t in texts:
        yield t
        await asyncio.sleep(delay)


async def _synth_once(
    tts: BailianCosyVoiceTTS, player, sink: list[bytes] | None
) -> tuple[float | None, float, int]:
    t0 = time.monotonic()
    total = 0
    async for audio in tts.stream_audio(_chunks(DEMO_CHUNKS)):
        if player is not None:
            player.write(audio)
        if sink is not None:
            sink.append(audio)
        total += len(audio)
    return tts.last_ttfa_ms, (time.monotonic() - t0) * 1000, total


async def _run(args: argparse.Namespace) -> None:
    from ...runtime.playback import StreamingPlayer

    tts = BailianCosyVoiceTTS(api_key=_load_api_key())
    sink: list[bytes] | None = [] if args.out else None
    player = None
    if args.out is None:
        try:
            player = StreamingPlayer(sample_rate=tts.sample_rate)
            player.write(b"\x00" * 4)  # 试开流
            player.stop()
        except Exception as e:
            print(f"[play] open failed: {e} — 无声卡降级为只统计", file=sys.stderr)
            player = None
    print(
        f"[cfg] model={tts.model} voice={tts.voice} format={tts.audio_format} "
        f"rate={tts.sample_rate} proxy=None(direct)"
    )
    try:
        for i in range(2):  # 第 1 句冷、第 2 句起热（WS 复用）
            ttfa, total_ms, nbytes = await _synth_once(tts, player, sink)
            tag = "cold" if i == 0 else "hot"
            print(
                f"[tts:{tag}] TTFA={ttfa:.0f}ms total={total_ms:.0f}ms "
                f"bytes={nbytes}"
            )
            if player is not None:
                while player.pending_seconds > 0:
                    await asyncio.sleep(0.05)
                print(f"[play] played {player.position_seconds:.1f}s")

        if args.cancel:
            print("[cancel] 起新一句并在首音后立即 cancel …")
            agen = tts.stream_audio(_chunks(DEMO_CHUNKS))
            got = 0
            async for audio in agen:
                got += 1
                if player is not None:
                    player.write(audio)
                if got == 1:
                    await tts.cancel()
                    if player is not None:
                        player.stop()
            print(f"[cancel] chunks before stop={got}，重开验证（同 socket 复用）…")
            ttfa2, total2, nb2 = await _synth_once(tts, player, sink)
            print(
                f"[reopen] TTFA={ttfa2:.0f}ms total={total2:.0f}ms bytes={nb2}"
            )
            if player is not None:
                while player.pending_seconds > 0:
                    await asyncio.sleep(0.05)
    finally:
        await tts.close()
        if player is not None:
            player.close()
    if args.out and sink:
        Path(args.out).write_bytes(b"".join(sink))
        print(f"[out] wrote {args.out} ({sum(map(len, sink))}B pcm)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cancel", action="store_true", help="演示 cancel+重开")
    parser.add_argument("--out", type=Path, default=None, help="落盘裸 PCM")
    args = parser.parse_args()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
