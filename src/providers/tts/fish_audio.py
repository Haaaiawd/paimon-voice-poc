"""Fish Audio 流式 TTS adapter：s2.1-pro-free，v1 WebSocket 协议。

协议（docs.fish.audio /api-reference/endpoint/websocket/tts-live）：
  connect(Authorization: Bearer, model: <model>) →
  msgpack {"event":"start","request":{text:"",format,latency,...}} →
  每个语义边界块 {"event":"text"} + {"event":"flush"} →
  文本流结束 {"event":"stop"} →
  服务端回 {"event":"audio","audio":<bin>}×N +
  {"event":"finish","reason":"stop"|"error"} 后主动关闭连接。

v1 一条连接一个会话（finish 后服务端关闭 socket），"WS 常驻"实现为预热：
每轮结束后后台预连下一条 socket，下一次 stream_audio 拿到已建好的热
连接——热 TTFA 口径即 stream_audio 调用 → 首字节音频，不含 TCP/TLS 握手。

cancel 契约（base.py docstring 三条）实现：
- 停推：关闭 socket，服务端生成随连接中止，接收循环立即退出；
- buffer 清理：接收即产即出，本地无滞留；socket 关闭后内核缓冲一并丢弃；
- 可重开：cancel 后立刻预连下一条 socket，下一次 stream_audio 是干净会话。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from typing import Any

import msgpack
import websockets
from websockets.asyncio.client import ClientConnection

from .base import TTSError, TTSProvider

DEFAULT_MODEL = "s2.1-pro-free"
DEFAULT_WS_URL = "wss://api.fish.audio/v1/tts/live"

# chinese-tts-eval C2：voice agent 场景用 balanced——normal 攒质量、low 最省延迟
# 但语句自然度掉档，balanced 是官方推荐的最低 TTFA 档。
DEFAULT_LATENCY = "balanced"
# chunk_length：服务端文本缓冲攒自然度的粒度；语义边界 flush 已强制出音频，
# 300 与官方 SDK 默认一致。
DEFAULT_CHUNK_LENGTH = 300


def _pack(obj: Mapping[str, Any]) -> bytes:
    return msgpack.packb(dict(obj), use_bin_type=True)


class FishAudioTTS(TTSProvider):
    """Fish Audio v1 WS 流式合成。

    `stream_audio(chunks)` 中每个 chunk 视为一个语义边界（Text Chunker
    已切好），映射为 text + flush；chunks 结束发 stop 收尾。
    `sample_rate` 属性即产出 PCM 的采样率（int16 mono），供播放侧开流用。
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        url: str = DEFAULT_WS_URL,
        audio_format: str = "pcm",
        sample_rate: int = 44100,
        latency: str = DEFAULT_LATENCY,
        chunk_length: int = DEFAULT_CHUNK_LENGTH,
        reference_id: str | None = None,
        request_params: Mapping[str, Any] | None = None,
        open_timeout: float = 15.0,
        proxy: str | None = None,
        connector: Any | None = None,
        prewarm: bool = True,
    ) -> None:
        if not api_key:
            raise TTSError("FishAudioTTS: api_key is required")
        self._api_key = api_key
        self.model = model
        self.url = url
        self.audio_format = audio_format
        self.sample_rate = sample_rate
        self.latency = latency
        self.chunk_length = chunk_length
        self.reference_id = reference_id
        self.request_params = dict(request_params or {})
        self.open_timeout = open_timeout
        # proxy=None → websockets 直连，绕开 env 里的 HTTP(S)_PROXY（Clash 坑）
        self.proxy = proxy
        # 测试可注入替代 connector（签名同 websockets.connect）
        self._connect = connector or websockets.connect
        self._prewarm = prewarm

        self._ws: ClientConnection | None = None
        self._warm_task: asyncio.Task[ClientConnection] | None = None
        self._closed = False
        # 世代计数：cancel 递增；stale 会话（gen != 当前值）不得再产出
        self._generation = 0
        # 延迟观测：stream_audio 进入 → 首字节的毫秒数（demo/bench 用）
        self.last_ttfa_ms: float | None = None
        # 上一轮 socket 是否来自预热（热路径证据，bench 用）
        self.last_acquire_warm: bool = False

    # ---- 连接管理 ----

    async def _open_ws(self) -> ClientConnection:
        try:
            return await self._connect(
                self.url,
                additional_headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "model": self.model,
                },
                open_timeout=self.open_timeout,
                proxy=self.proxy,
            )
        except Exception as e:
            raise TTSError(f"FishAudioTTS: connect failed: {e}") from e

    def _schedule_prewarm(self) -> None:
        """本轮 socket 用掉后，后台预连下一条，让下一次调用走热连接。"""
        if self._closed or not self._prewarm:
            return
        if self._warm_task is None or self._warm_task.done():
            self._warm_task = asyncio.create_task(self._open_ws())
            # 预热失败不炸任务，留待 _acquire 里重试/透传
            self._warm_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def _acquire(self) -> ClientConnection:
        """取一条可用 socket：优先预热好的，失败则现场重连一次。"""
        task, self._warm_task = self._warm_task, None
        ws: ClientConnection | None = None
        if task is not None:
            try:
                ws = await task  # connect 自身带 open_timeout，不会久等
            except BaseException:
                ws = None  # 预热失败/被取消 → 同步重连
        self.last_acquire_warm = ws is not None
        if ws is None:
            ws = await self._open_ws()
        self._ws = ws
        return ws

    def _start_request(self) -> dict[str, Any]:
        req: dict[str, Any] = {
            "text": "",
            "format": self.audio_format,
            "sample_rate": self.sample_rate,
            "latency": self.latency,
            "chunk_length": self.chunk_length,
            **self.request_params,
        }
        if self.reference_id is not None:
            req.setdefault("reference_id", self.reference_id)
        return req

    # ---- TTSProvider ----

    async def stream_audio(
        self,
        chunks: AsyncIterable[str],
        **kwargs: Any,
    ) -> AsyncIterator[bytes]:
        if self._closed:
            raise TTSError("FishAudioTTS is closed")
        gen = self._generation
        self.last_ttfa_ms = None
        t0 = time.monotonic()

        ws = await self._acquire()
        finished = False
        pump_error: list[Exception] = []

        async def pump() -> None:
            try:
                async for text in chunks:
                    if not text:
                        continue
                    await ws.send(_pack({"event": "text", "text": text}))
                    # C2：语义边界强制 flush，不等服务端攒够缓冲
                    await ws.send(_pack({"event": "flush"}))
                await ws.send(_pack({"event": "stop"}))
            except websockets.ConnectionClosed:
                pass  # 接收侧以 finish/错误收尾
            except Exception as e:
                pump_error.append(e)
                await ws.close()  # 文本源挂了 → 让接收侧退出

        sender = asyncio.create_task(pump())
        try:
            await ws.send(_pack({"event": "start", "request": self._start_request()}))
            async for raw in ws:
                if isinstance(raw, str):
                    continue  # 协议外文本帧忽略
                if gen != self._generation:
                    break  # cancel 已关 socket，防御性退出
                msg = msgpack.unpackb(raw, raw=False)
                event = msg.get("event")
                if event == "audio":
                    audio = msg.get("audio") or b""
                    if self.last_ttfa_ms is None:
                        self.last_ttfa_ms = (time.monotonic() - t0) * 1000
                    yield bytes(audio)
                elif event == "finish":
                    finished = True
                    if msg.get("reason") == "error":
                        raise TTSError(
                            f"fish session error: {json.dumps(msg, ensure_ascii=False)}"
                        )
                    break
                # 其余事件（warning/usage 等）忽略
        except websockets.ConnectionClosed as e:
            if gen != self._generation or finished:
                pass  # cancel 主动断连 / finish 后服务端关闭，均为正常收尾
            else:
                raise TTSError(
                    f"connection closed before finish: {e}"
                ) from e
        finally:
            if not sender.done():
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            self._ws = None
            self._schedule_prewarm()

        if pump_error:
            raise TTSError(f"text source failed: {pump_error[0]}") from pump_error[0]
        if not finished and gen == self._generation:
            raise TTSError("stream ended without finish event")

    async def cancel(self) -> None:
        """打断当前合成：关 socket 停推，预连下一条。幂等。"""
        self._generation += 1
        ws, self._ws = self._ws, None
        if ws is not None:
            await ws.close()
        self._schedule_prewarm()

    async def close(self) -> None:
        """释放连接与预热任务；幂等。"""
        self._closed = True
        task, self._warm_task = self._warm_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        ws, self._ws = self._ws, None
        if ws is not None:
            await ws.close()
