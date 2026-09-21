"""麦克风采集：sounddevice RawInputStream → asyncio.Queue 的 int16 PCM 帧。

Windows 官方 wheel 自带 PortAudio（TASK-001 选型理由），16kHz mono int16
与 Silero VAD / Smart Turn 的输入约定一致；blocksize=512 即 32ms@16kHz，
正好是 Silero 单帧分析所需帧数。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import sounddevice as sd


class MicCapture:
    """持续采集麦克风 PCM 帧；`frames()` 异步迭代消费。"""

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        channels: int = 1,
        blocksize: int = 512,
        device: int | str | None = None,
        max_queue: int = 64,
    ):
        self._sample_rate = sample_rate
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=max_queue)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream = sd.RawInputStream(
            samplerate=sample_rate,
            channels=channels,
            dtype="int16",
            blocksize=blocksize,
            device=device,
            callback=self._on_audio,
        )

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def start(self) -> None:
        """开始采集；须在已运行的 asyncio loop 所在线程调用。"""
        self._loop = asyncio.get_running_loop()
        self._stream.start()

    def stop(self) -> None:
        self._stream.stop()
        self._stream.close()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)

    async def frames(self) -> AsyncIterator[bytes]:
        """产出 PCM bytes；stop() 后迭代结束。"""
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    def _on_audio(self, indata, frames: int, time_info, status) -> None:
        # PortAudio 回调线程：只投递，不做分析。队列满时丢最旧帧保实时性。
        if self._loop is None:
            return
        chunk = bytes(indata)
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._loop.call_soon_threadsafe(self._queue.put_nowait, chunk)
