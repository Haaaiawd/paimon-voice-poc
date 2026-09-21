"""流式播放器：write() 增量喂 PCM，stop() 立即清 buffer 并回报实际播放位置。

doc 02 §Audio Playback 契约：
- 流式播放（write 追加、PortAudio 回调线程消费）；
- stop() 立即生效——清空尚未播放的 buffer + abort 输出流，不放残音；
- 能知道"实际播到了哪里"：_played_bytes 只统计回调真实取走的音频字节
  （不含补零），position_seconds 即已播出秒数；误差≈PortAudio 输出延迟，
  是用户实际听到内容的最佳可观测近似，供 heard history 使用。

stop() 后可再次 write()：自动开新流，位置计数随新一轮归零。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import sounddevice as sd

StreamFactory = Callable[[Callable], Any]


class StreamingPlayer:
    """int16 PCM 流式播放 + 可观测位置 + 立即停播。"""

    def __init__(
        self,
        *,
        sample_rate: int = 24000,
        channels: int = 1,
        dtype: str = "int16",
        blocksize: int = 0,
        latency: str | float = "low",
        device: int | str | None = None,
        stream_factory: StreamFactory | None = None,
    ):
        if dtype != "int16":
            raise ValueError("StreamingPlayer only supports int16 PCM")
        self._sample_rate = sample_rate
        self._frame_bytes = channels * 2
        self._bytes_per_sec = sample_rate * self._frame_bytes
        self._device = device
        self._latency = latency
        self._blocksize = blocksize
        self._stream_factory = stream_factory

        self._lock = threading.Lock()
        self._buf = bytearray()
        self._played_bytes = 0
        self._stream: Any = None
        self._closed = False

    # ---- 状态观测 ----

    @property
    def position_seconds(self) -> float:
        """已真实送进输出流的音频时长（秒）。"""
        with self._lock:
            return self._played_bytes / self._bytes_per_sec

    @property
    def pending_seconds(self) -> float:
        """已写入但尚未播出的时长（秒）。"""
        with self._lock:
            return len(self._buf) / self._bytes_per_sec

    @property
    def is_playing(self) -> bool:
        return self._stream is not None

    # ---- 写入与停止 ----

    def write(self, pcm: bytes) -> None:
        """追加一段 PCM；首次写入时惰性打开输出流。"""
        if not pcm:
            return
        with self._lock:
            if self._closed:
                raise RuntimeError("StreamingPlayer is closed")
            self._buf += pcm
            if self._stream is None:
                self._open_stream()

    def stop(self) -> float:
        """立即停播：清 buffer + abort 流，返回实际已播秒数。"""
        with self._lock:
            played = self._played_bytes / self._bytes_per_sec
            self._buf.clear()
            self._played_bytes = 0
            stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.abort()
            finally:
                stream.close()
        return played

    def close(self) -> None:
        """停止并关闭；之后 write() 抛错。"""
        self.stop()
        with self._lock:
            self._closed = True

    # ---- 内部 ----

    def _open_stream(self) -> None:
        if self._stream_factory is not None:
            self._stream = self._stream_factory(self._callback)
        else:
            self._stream = sd.RawOutputStream(
                samplerate=self._sample_rate,
                channels=1,
                dtype="int16",
                blocksize=self._blocksize,
                latency=self._latency,
                device=self._device,
                callback=self._callback,
            )
        self._stream.start()

    def _callback(self, outdata, frames: int, time_info, status) -> None:
        need = frames * self._frame_bytes
        with self._lock:
            n = min(need, len(self._buf))
            if n:
                outdata[:n] = self._buf[:n]
                del self._buf[:n]
                self._played_bytes += n
        if n < need:
            outdata[n:need] = b"\x00" * (need - n)
