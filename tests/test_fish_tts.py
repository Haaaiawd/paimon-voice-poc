"""TASK-008：FishAudioTTS adapter 对 fake Fish Audio WebSocket 服务的协议与行为测试。

verify_by: pytest tests/test_fish_tts.py 通过。

fake server 按 v1 协议走：start → [text,flush]×N → stop → audio×N →
finish → 服务端关闭（一条连接一个会话）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import msgpack
import pytest
from websockets.asyncio.server import serve

from providers.tts import FishAudioTTS, TTSError


def _pack(obj: dict) -> bytes:
    return msgpack.packb(obj, use_bin_type=True)


class FakeFish:
    """记录客户端事件并按脚本应答的假 Fish Audio 端点。"""

    def __init__(self) -> None:
        self.auth: str | None = None
        self.model: str | None = None
        self.events: list[dict] = []
        self.connections = 0
        self.url = ""
        # 每次 flush 弹一组 audio 帧回客户端
        self._audio_per_flush: list[list[bytes]] = []

    def script_audio(self, audio_per_flush: list[list[bytes]]) -> None:
        self._audio_per_flush = [list(g) for g in audio_per_flush]

    async def handler(self, ws) -> None:
        self.connections += 1
        self.auth = ws.request.headers.get("authorization")
        self.model = ws.request.headers.get("model")
        async for raw in ws:
            if isinstance(raw, str):
                continue
            msg = msgpack.unpackb(raw, raw=False)
            self.events.append(msg)
            event = msg.get("event")
            if event == "flush" and self._audio_per_flush:
                for chunk in self._audio_per_flush.pop(0):
                    await ws.send(_pack({"event": "audio", "audio": chunk}))
            elif event == "stop":
                await ws.send(_pack({"event": "finish", "reason": "stop"}))
                return  # 服务端主动关（v1：一条连接一个会话）

    async def __aenter__(self) -> "FakeFish":
        self._server = await serve(self.handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.close()
        await self._server.wait_closed()


async def _texts(*parts: str) -> AsyncIterator[str]:
    for p in parts:
        yield p


def _tts(url: str, **kw) -> FishAudioTTS:
    return FishAudioTTS(api_key="test-key", url=url, **kw)


async def test_stream_protocol_and_audio():
    """完整握手 + 语义边界 text+flush + stop 收尾 + audio 下行。"""
    async with FakeFish() as fake:
        fake.script_audio([[b"A1"], [b"B1", b"B2"]])
        tts = _tts(fake.url)
        chunks = [
            c
            async for c in tts.stream_audio(_texts("你好。", "旅行者！"))
        ]

    assert chunks == [b"A1", b"B1", b"B2"]
    assert fake.auth == "Bearer test-key"
    assert fake.model == "s2.1-pro-free"
    kinds = [e["event"] for e in fake.events]
    assert kinds == ["start", "text", "flush", "text", "flush", "stop"]
    start = fake.events[0]["request"]
    assert start["text"] == ""
    assert start["format"] == "pcm"
    assert start["latency"] == "balanced"
    assert fake.events[1]["text"] == "你好。"
    assert fake.events[3]["text"] == "旅行者！"
    assert tts.last_ttfa_ms is not None


async def test_cancel_stops_stream_and_reopens_clean():
    """cancel 契约：停推（cancel 后无新 chunk）+ 可立刻重开干净会话。"""
    async with FakeFish() as fake:
        # 每个 flush 回很多帧，保证 cancel 时还有残余在飞
        fake.script_audio([[b"x"] * 8, [b"y"], [b"z"]])
        tts = _tts(fake.url)

        got: list[bytes] = []
        async for audio in tts.stream_audio(_texts("第一句。", "第二句。")):
            got.append(audio)
            if len(got) == 1:
                await tts.cancel()
        assert got == [b"x"]  # cancel 后没有任何新 chunk 产出

        # 立刻重开：全新会话（v1 一条连接一个会话 → 新 socket）
        fake.events.clear()
        fake.script_audio([[b"Q1"]])
        chunks = [c async for c in tts.stream_audio(_texts("重来。"))]
        assert chunks == [b"Q1"]
        assert fake.connections == 2
        assert fake.events[0]["event"] == "start"  # 干净会话从头握手


async def test_prewarm_socket_reused_for_next_call():
    """一轮结束后后台预连：下一轮 stream_audio 直接用已建好的热连接。"""
    async with FakeFish() as fake:
        fake.script_audio([[b"a"]])
        tts = _tts(fake.url)
        _ = [c async for c in tts.stream_audio(_texts("一。"))]
        await asyncio.sleep(0.1)  # 给预连一个调度窗口
        assert fake.connections == 2  # 预连已建好，第二轮还没开始

        fake.script_audio([[b"b"]])
        chunks = [c async for c in tts.stream_audio(_texts("二。"))]
        assert chunks == [b"b"]
        assert fake.connections == 2  # 第二轮复用预热连接，没新建


async def test_finish_error_surfaces():
    """finish reason=error → TTSError，已收到的音频保留。"""

    async def err_server(ws) -> None:
        async for raw in ws:
            msg = msgpack.unpackb(raw, raw=False)
            if msg.get("event") == "flush":
                await ws.send(_pack({"event": "audio", "audio": b"part"}))
            elif msg.get("event") == "stop":
                await ws.send(
                    _pack(
                        {
                            "event": "finish",
                            "reason": "error",
                            "detail": "quota",
                        }
                    )
                )
                return

    async with serve(err_server, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        tts = _tts(f"ws://127.0.0.1:{port}")
        got: list[bytes] = []
        with pytest.raises(TTSError, match="error"):
            async for audio in tts.stream_audio(_texts("boom")):
                got.append(audio)
        assert got == [b"part"]


async def test_close_before_finish_raises():
    """服务端不发 finish 直接断连 → TTSError。"""

    async def rude(ws) -> None:
        async for raw in ws:
            msg = msgpack.unpackb(raw, raw=False)
            if msg.get("event") == "stop":
                return  # 直接关，不发 finish

    async with serve(rude, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        tts = _tts(f"ws://127.0.0.1:{port}")
        with pytest.raises(TTSError):
            _ = [c async for c in tts.stream_audio(_texts("x"))]


async def test_env_proxy_is_bypassed(monkeypatch):
    """proxy=None 直连：env 里指向死端口的代理不得生效（Clash 坑回归）。"""
    for var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "WS_PROXY",
        "WSS_PROXY",
        "http_proxy",
        "https_proxy",
        "ws_proxy",
        "wss_proxy",
    ):
        monkeypatch.setenv(var, "http://127.0.0.1:1")
    async with FakeFish() as fake:
        fake.script_audio([[b"ok"]])
        tts = _tts(fake.url)
        chunks = [c async for c in tts.stream_audio(_texts("直连。"))]
    assert chunks == [b"ok"]


async def test_cancel_idempotent_and_close():
    async with FakeFish() as fake:
        tts = _tts(fake.url)
        await tts.cancel()  # 无活动会话，幂等
        await tts.cancel()
        await tts.close()
        await tts.close()
        with pytest.raises(TTSError):
            _ = [c async for c in tts.stream_audio(_texts("x"))]


def test_api_key_required():
    with pytest.raises(TTSError):
        FishAudioTTS(api_key="")


def test_request_params_override():
    tts = FishAudioTTS(
        api_key="k",
        sample_rate=24000,
        reference_id="ref-1",
        request_params={"latency": "low", "temperature": 0.5},
    )
    req = tts._start_request()
    assert req["latency"] == "low"
    assert req["temperature"] == 0.5
    assert req["sample_rate"] == 24000
    assert req["reference_id"] == "ref-1"
