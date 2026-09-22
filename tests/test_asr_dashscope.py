"""TASK-007：DashScopeASR adapter 对 fake DashScope WebSocket 服务的协议与事件测试。

verify_by: pytest tests/test_asr_dashscope.py 通过。

fake server 按真实协议走：run-task → task-started → 收二进制帧 →
result-generated → finish-task → task-finished。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from websockets.asyncio.server import serve

from providers.asr import ASRError, ASREvent, DashScopeASR


def _event_msg(event: str, task_id: str, payload: dict | None = None) -> str:
    return json.dumps(
        {
            "header": {"event": event, "task_id": task_id},
            "payload": payload or {},
        },
        ensure_ascii=False,
    )


def _sentence(text: str, *, end: bool = False, heartbeat: bool = False) -> dict:
    return {
        "output": {
            "sentence": {
                "text": text,
                "begin_time": 0,
                "end_time": 1000 if end else None,
                "sentence_end": end,
                "heartbeat": heartbeat,
            }
        }
    }


class FakeDashScope:
    """记录客户端行为并按脚本应答的假 DashScope 端点。"""

    def __init__(self) -> None:
        self.auth: str | None = None
        self.run_task: dict | None = None
        self.frames: list[bytes] = []
        self.finish_task = False
        self._replies: list[str] = []
        self.url = ""

    def script_results(self, replies: list[str]) -> None:
        """每收到一帧二进制音频，弹出一条消息回给客户端。"""
        self._replies = list(replies)

    async def handler(self, ws) -> None:
        self.auth = ws.request.headers.get("authorization")
        task_id = ""
        async for raw in ws:
            if isinstance(raw, bytes):
                self.frames.append(bytes(raw))
                if self._replies:
                    await ws.send(self._replies.pop(0))
                continue
            msg = json.loads(raw)
            action = msg["header"].get("action")
            task_id = msg["header"].get("task_id", task_id)
            if action == "run-task":
                self.run_task = msg
                await ws.send(_event_msg("task-started", task_id))
            elif action == "finish-task":
                self.finish_task = True
                while self._replies:  # 收尾前把剩余结果都吐出来
                    await ws.send(self._replies.pop(0))
                await ws.send(_event_msg("task-finished", task_id))

    async def __aenter__(self) -> "FakeDashScope":
        self._server = await serve(self.handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.close()
        await self._server.wait_closed()


async def _audio(*frames: bytes) -> AsyncIterator[bytes]:
    for f in frames:
        yield f


def _asr(url: str, **kw) -> DashScopeASR:
    return DashScopeASR(api_key="test-key", url=url, **kw)


async def test_stream_events_and_protocol_envelope():
    """完整握手 + 音频上行 + partial/final 映射 + finish-task 收尾。"""
    async with FakeDashScope() as fake:
        fake.script_results(
            [
                _event_msg("result-generated", "", _sentence("你好")),
                _event_msg("result-generated", "", _sentence("", heartbeat=True)),
                _event_msg("result-generated", "", _sentence("")),
                _event_msg("result-generated", "", _sentence("你好，派")),
                _event_msg("result-generated", "", _sentence("你好，派蒙", end=True)),
            ]
        )
        asr = _asr(fake.url)
        events = [
            e async for e in asr.stream(_audio(b"a" * 320, b"b" * 320, b"c" * 320))
        ]

    # 事件面（C3）：partial/final，心跳与空文本被过滤
    assert [(e.kind, e.text) for e in events] == [
        ("partial", "你好"),
        ("partial", "你好，派"),
        ("final", "你好，派蒙"),
    ]
    # run-task 信封与参数（C1/C2/C4 + 协议字段）
    assert fake.auth == "Bearer test-key"
    assert fake.run_task["header"]["action"] == "run-task"
    assert fake.run_task["header"]["streaming"] == "duplex"
    assert fake.run_task["header"]["task_id"]
    payload = fake.run_task["payload"]
    assert payload["task_group"] == "audio"
    assert payload["task"] == "asr"
    assert payload["function"] == "recognition"
    assert payload["model"] == "paraformer-realtime-v2"
    params = payload["parameters"]
    assert params["semantic_punctuation_enabled"] is False
    assert params["max_sentence_silence"] == 500
    assert params["disfluency_removal_enabled"] is False
    assert params["language_hints"] == ["zh"]
    assert params["heartbeat"] is True
    assert params["sample_rate"] == 16000
    assert params["format"] == "pcm"
    # 音频按原样二进制上行；audio 结束发 finish-task
    assert fake.frames == [b"a" * 320, b"b" * 320, b"c" * 320]
    assert fake.finish_task is True


async def test_sentence_end_flag_also_marks_final():
    """v2 的 sentence_end=true（end_time 为 None）同样映射 final。"""
    async with FakeDashScope() as fake:
        sentence = {
            "output": {
                "sentence": {
                    "text": "结束",
                    "begin_time": 0,
                    "end_time": None,
                    "sentence_end": True,
                }
            }
        }
        fake.script_results([_event_msg("result-generated", "", sentence)])
        asr = _asr(fake.url)
        events = [e async for e in asr.stream(_audio(b"x" * 320))]
    assert [(e.kind, e.text) for e in events] == [("final", "结束")]


async def test_task_failed_on_handshake_raises():
    async def refuse(ws):
        async for raw in ws:
            msg = json.loads(raw)
            tid = msg["header"].get("task_id", "")
            await ws.send(
                json.dumps(
                    {
                        "header": {
                            "event": "task-failed",
                            "task_id": tid,
                            "error_code": "InvalidParameter",
                            "error_message": "model not supported",
                        }
                    }
                )
            )
            return

    async with serve(refuse, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        asr = _asr(f"ws://127.0.0.1:{port}")
        with pytest.raises(ASRError, match="InvalidParameter"):
            _ = [e async for e in asr.stream(_audio(b"x" * 320))]


async def test_task_failed_midstream_raises():
    """task-started 后中途 task-failed：已发事件保留，错误透出。"""
    async def boom_after_first(ws):
        frames = 0
        task_id = ""
        async for raw in ws:
            if isinstance(raw, bytes):
                frames += 1
                if frames == 1:
                    await ws.send(
                        _event_msg("result-generated", task_id, _sentence("你"))
                    )
                elif frames == 2:
                    await ws.send(
                        json.dumps(
                            {
                                "header": {
                                    "event": "task-failed",
                                    "task_id": task_id,
                                    "error_code": "InternalError",
                                    "error_message": "boom",
                                }
                            }
                        )
                    )
                continue
            msg = json.loads(raw)
            task_id = msg["header"].get("task_id", task_id)
            if msg["header"].get("action") == "run-task":
                await ws.send(_event_msg("task-started", task_id))

    async with serve(boom_after_first, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        asr = _asr(f"ws://127.0.0.1:{port}")
        got: list[ASREvent] = []
        with pytest.raises(ASRError, match="boom"):
            async for ev in asr.stream(_audio(*[b"x" * 320] * 3)):
                got.append(ev)
        assert [e.text for e in got] == ["你"]


async def test_abnormal_close_raises_asr_error():
    """服务端未发 task-finished 直接断连 → ASRError。"""

    async def rude(ws):
        async for raw in ws:
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            if msg["header"].get("action") == "run-task":
                await ws.send(_event_msg("task-started", msg["header"]["task_id"]))
            elif msg["header"].get("action") == "finish-task":
                return  # 直接关，不发 task-finished

    async with serve(rude, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        asr = _asr(f"ws://127.0.0.1:{port}")
        with pytest.raises(ASRError, match="before task-finished"):
            _ = [e async for e in asr.stream(_audio(b"x" * 320))]


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
    async with FakeDashScope() as fake:
        fake.script_results([_event_msg("result-generated", "", _sentence("直连"))])
        asr = _asr(fake.url)
        events = [e async for e in asr.stream(_audio(b"x" * 320))]
    assert [e.text for e in events] == ["直连"]


async def test_audio_source_error_surfaces():
    async def bad_audio() -> AsyncIterator[bytes]:
        yield b"x" * 320
        raise RuntimeError("mic died")

    async with FakeDashScope() as fake:
        asr = _asr(fake.url)
        with pytest.raises(ASRError, match="mic died"):
            _ = [e async for e in asr.stream(bad_audio())]


async def test_close_is_idempotent():
    async with FakeDashScope() as fake:
        asr = _asr(fake.url)
        await asr.close()  # 无活动 stream，幂等
        await asr.close()


def test_parameters_merge_and_key_required():
    with pytest.raises(ASRError):
        DashScopeASR(api_key="")
    asr = DashScopeASR(api_key="k", parameters={"max_sentence_silence": 800, "custom": 1})
    assert asr.parameters["max_sentence_silence"] == 800
    assert asr.parameters["custom"] == 1
    assert asr.parameters["semantic_punctuation_enabled"] is False
