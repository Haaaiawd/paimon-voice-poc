"""TASK-008：BailianCosyVoiceTTS adapter 对 fake DashScope WS 服务的协议与行为测试。

verify_by: pytest tests/test_bailian_tts.py 通过。

fake server 按真实协议走：run-task → task-started → continue-task(文本)
→ 二进制音频帧 → finish-task → task-finished；连接保持，多任务复用。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from websockets.asyncio.server import serve

from providers.tts import BailianCosyVoiceTTS, TTSError


def _event(event: str, task_id: str, payload: dict | None = None) -> str:
    return json.dumps(
        {"header": {"event": event, "task_id": task_id}, "payload": payload or {}},
        ensure_ascii=False,
    )


class FakeBailian:
    """记录客户端行为并按脚本应答的假百炼端点；连接常驻，任务可复用。"""

    def __init__(self) -> None:
        self.auth: str | None = None
        self.connections = 0
        self.run_tasks: list[dict] = []
        self.continue_texts: list[str] = []
        self.finishes: list[dict] = []  # finish-task 的 payload.input
        self.url = ""
        # 每条 continue-task 弹一组音频帧；None 表示不回
        self._audio_per_text: list[list[bytes] | None] = []
        self._text_delay = 0.0

    def script_audio(
        self, audio_per_text: list[list[bytes] | None], delay: float = 0.0
    ) -> None:
        self._audio_per_text = list(audio_per_text)
        self._text_delay = delay

    async def handler(self, ws) -> None:
        self.connections += 1
        self.auth = ws.request.headers.get("authorization")
        async for raw in ws:
            if isinstance(raw, bytes):
                continue  # TTS 客户端不上行二进制
            msg = json.loads(raw)
            action = msg["header"].get("action")
            task_id = msg["header"].get("task_id", "")
            if action == "run-task":
                self.run_tasks.append(msg)
                await ws.send(_event("task-started", task_id))
            elif action == "continue-task":
                self.continue_texts.append(msg["payload"]["input"]["text"])
                if self._audio_per_text:
                    group = self._audio_per_text.pop(0)
                    if group:
                        for frame in group:
                            await ws.send(frame)
                            if self._text_delay:
                                await asyncio.sleep(self._text_delay)
            elif action == "finish-task":
                self.finishes.append(msg["payload"].get("input", {}))
                await ws.send(_event("task-finished", task_id))

    async def __aenter__(self) -> "FakeBailian":
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


class _CloseOnFirstFinish(FakeBailian):
    """第一条连接在 finish-task 时干净关闭（不回 task-finished）；
    后续连接正常——模拟服务端在出声前回收空闲连接。"""

    async def handler(self, ws) -> None:
        conn_no = self.connections = self.connections + 1
        self.auth = ws.request.headers.get("authorization")
        async for raw in ws:
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            action = msg["header"].get("action")
            task_id = msg["header"].get("task_id", "")
            if action == "run-task":
                self.run_tasks.append(msg)
                await ws.send(_event("task-started", task_id))
            elif action == "continue-task":
                self.continue_texts.append(msg["payload"]["input"]["text"])
                if conn_no > 1:
                    for frame in [b"PCM-RETRY"]:
                        await ws.send(frame)
            elif action == "finish-task":
                self.finishes.append(msg["payload"].get("input", {}))
                if conn_no == 1:
                    await ws.close(1000)  # 出声前干净关闭
                    return
                await ws.send(_event("task-finished", task_id))


class _CloseAfterAudio(FakeBailian):
    """回过音频后干净关闭：半路截断，模拟播放中途掉线。"""

    async def handler(self, ws) -> None:
        self.connections += 1
        self.auth = ws.request.headers.get("authorization")
        async for raw in ws:
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            action = msg["header"].get("action")
            task_id = msg["header"].get("task_id", "")
            if action == "run-task":
                self.run_tasks.append(msg)
                await ws.send(_event("task-started", task_id))
            elif action == "continue-task":
                self.continue_texts.append(msg["payload"]["input"]["text"])
                await ws.send(b"PCM-1")
                await ws.close(1000)  # 出了声才断
                return
            elif action == "finish-task":
                await ws.send(_event("task-finished", task_id))


async def _slow_texts(*parts: str, delay: float = 0.05) -> AsyncIterator[str]:
    """带间隔的文本流：模拟 LLM token 流，让 cancel 落在 pump 未发完时。"""
    for p in parts:
        yield p
        await asyncio.sleep(delay)


def _tts(url: str, **kw) -> BailianCosyVoiceTTS:
    return BailianCosyVoiceTTS(api_key="test-key", url=url, **kw)


async def test_stream_protocol_and_audio():
    """run-task 信封 + continue-task 分句 + finish-task 收尾 + 二进制下行。"""
    async with FakeBailian() as fake:
        fake.script_audio([[b"A1"], [b"B1", b"B2"]])
        tts = _tts(fake.url)
        chunks = [
            c
            async for c in tts.stream_audio(_texts("你好。", "旅行者！"))
        ]
        await tts.close()

    assert chunks == [b"A1", b"B1", b"B2"]
    assert fake.auth == "Bearer test-key"
    assert len(fake.run_tasks) == 1
    run = fake.run_tasks[0]
    assert run["header"]["action"] == "run-task"
    assert run["header"]["streaming"] == "duplex"
    payload = run["payload"]
    assert payload["task_group"] == "audio"
    assert payload["task"] == "tts"
    assert payload["function"] == "SpeechSynthesizer"
    assert payload["model"] == "cosyvoice-v3-flash"
    params = payload["parameters"]
    assert params["voice"] == "longhuhu_v3"
    assert params["format"] == "pcm"
    assert params["sample_rate"] == 24000
    assert params["text_type"] == "PlainText"
    assert fake.continue_texts == ["你好。", "旅行者！"]
    assert fake.finishes == [{}]  # 正常收尾不带 directive
    assert tts.last_ttfa_ms is not None


async def test_persistent_connection_reused_across_tasks():
    """WS 常驻：两轮 stream_audio 走同一条 socket（热 TTFA 前提）。"""
    async with FakeBailian() as fake:
        fake.script_audio([[b"a"]])
        tts = _tts(fake.url)
        _ = [c async for c in tts.stream_audio(_texts("一。"))]
        fake.script_audio([[b"b"]])
        chunks = [c async for c in tts.stream_audio(_texts("二。"))]
        await tts.close()

    assert chunks == [b"b"]
    assert fake.connections == 1  # 连接复用，没有重连
    assert len(fake.run_tasks) == 2
    assert (
        fake.run_tasks[0]["header"]["task_id"]
        != fake.run_tasks[1]["header"]["task_id"]
    )


async def test_cancel_stops_stream_and_reopens_on_same_socket():
    """cancel 契约：停推 + directive=cancel 上行 + 排空后同 socket 重开。"""
    async with FakeBailian() as fake:
        # 每帧间留空隙，保证 cancel 落地时还有残余帧在路上
        fake.script_audio(
            [[b"x"] * 8, [b"y"], [b"z"]], delay=0.01
        )
        tts = _tts(fake.url, cancel_timeout=2.0)

        got: list[bytes] = []
        async for audio in tts.stream_audio(
            _slow_texts("第一句。", "第二句。")
        ):
            got.append(audio)
            if len(got) == 1:
                await tts.cancel()
        assert got == [b"x"]  # cancel 后没有任何新 chunk 产出
        # 服务端确实收到了 cancel 指令
        assert fake.finishes[-1].get("directive") == "cancel"

        # 立刻重开：排空完成 → 同一条常驻 socket 直接复用
        fake.script_audio([[b"Q1"]])
        chunks = [c async for c in tts.stream_audio(_texts("重来。"))]
        assert chunks == [b"Q1"]
        assert fake.connections == 1
        assert len(fake.run_tasks) == 2


async def test_cancel_during_yield_parked_also_works():
    """barge-in 真实形态：生成器停在 yield，cancel() 自己排空 socket。"""
    async with FakeBailian() as fake:
        fake.script_audio([[b"x"] * 4, [b"z"]], delay=0.02)
        tts = _tts(fake.url, cancel_timeout=2.0)

        agen = tts.stream_audio(_texts("第一句。", "第二句。"))
        first = await agen.__anext__()  # 拿一帧后停住（生成器停在 yield）
        assert first == b"x"
        await tts.cancel()  # 此刻无人迭代，cancel 亲自排空到 task-finished

        rest = [c async for c in agen]
        assert rest == []  # 残音没有漏给消费方

        fake.script_audio([[b"ok"]])
        chunks = [c async for c in tts.stream_audio(_texts("下一句。"))]
        assert chunks == [b"ok"]
        assert fake.connections == 1
        await tts.close()


async def test_task_failed_raises():
    async def refuse(ws) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            tid = msg["header"].get("task_id", "")
            if msg["header"].get("action") == "run-task":
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

    async with serve(refuse, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        tts = _tts(f"ws://127.0.0.1:{port}")
        with pytest.raises(TTSError, match="InvalidParameter"):
            _ = [c async for c in tts.stream_audio(_texts("x"))]


async def test_task_failed_midstream_raises():
    """task-started 后中途 task-failed：已发音频保留，错误透出。"""

    async def boom(ws) -> None:
        texts = 0
        task_id = ""
        async for raw in ws:
            msg = json.loads(raw)
            task_id = msg["header"].get("task_id", task_id)
            action = msg["header"].get("action")
            if action == "run-task":
                await ws.send(_event("task-started", task_id))
            elif action == "continue-task":
                texts += 1
                if texts == 1:
                    await ws.send(b"partial-audio")
                else:
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

    async with serve(boom, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        tts = _tts(f"ws://127.0.0.1:{port}")
        got: list[bytes] = []
        with pytest.raises(TTSError, match="boom"):
            async for audio in tts.stream_audio(_texts("一。", "二。")):
                got.append(audio)
        assert got == [b"partial-audio"]


async def test_abnormal_close_raises_tts_error():
    """服务端不发 task-finished 直接断连 → TTSError。"""

    async def rude(ws) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            action = msg["header"].get("action")
            if action == "run-task":
                await ws.send(
                    _event("task-started", msg["header"]["task_id"])
                )
            elif action == "finish-task":
                await ws.close(1011)  # 非正常关闭（1000 属干净关闭，走重试/截断）
                return

    async with serve(rude, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        tts = _tts(f"ws://127.0.0.1:{port}")
        with pytest.raises(TTSError, match="before task-finished"):
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
    async with FakeBailian() as fake:
        fake.script_audio([[b"ok"]])
        tts = _tts(fake.url)
        chunks = [c async for c in tts.stream_audio(_texts("直连。"))]
        await tts.close()
    assert chunks == [b"ok"]


async def test_cancel_idempotent_and_close():
    async with FakeBailian() as fake:
        tts = _tts(fake.url)
        await tts.cancel()  # 无活动任务，幂等
        await tts.cancel()
        await tts.close()
        await tts.close()
        with pytest.raises(TTSError):
            _ = [c async for c in tts.stream_audio(_texts("x"))]


def test_api_key_required():
    with pytest.raises(TTSError):
        BailianCosyVoiceTTS(api_key="")


def test_parameters_and_instruction():
    tts = BailianCosyVoiceTTS(
        api_key="k",
        voice="longhuhu_v3",
        sample_rate=16000,
        instruction="你说话的情感是happy。",
        parameters={"rate": 1.2},
    )
    run = json.loads(tts._run_task_message("t1"))
    params = run["payload"]["parameters"]
    assert params["voice"] == "longhuhu_v3"
    assert params["sample_rate"] == 16000
    assert params["rate"] == 1.2
    assert params["instruction"] == "你说话的情感是happy。"
    assert run["header"]["task_id"] == "t1"


async def test_clean_close_before_audio_retries_on_new_connection():
    """服务端 1000 干净关闭且未出声：换新连接把整段文本重放一次，
    调用方无感拿到音频。"""
    async with _CloseOnFirstFinish() as fake:
        tts = _tts(fake.url)
        chunks = [c async for c in tts.stream_audio(_texts("你好。", "旅行者！"))]
        await tts.close()

    assert chunks == [b"PCM-RETRY", b"PCM-RETRY"]
    assert fake.connections == 2
    # 文本在两条连接上各发了一次（重放证据）
    assert fake.continue_texts == ["你好。", "旅行者！", "你好。", "旅行者！"]
    assert len(fake.run_tasks) == 2


async def test_clean_close_after_audio_truncates_gracefully():
    """已出过声的干净关闭：按截断收尾——拿到已收音频，不抛异常。"""
    async with _CloseAfterAudio() as fake:
        tts = _tts(fake.url)
        chunks = [c async for c in tts.stream_audio(_texts("你好。", "旅行者！"))]
        await tts.close()

    assert chunks == [b"PCM-1"]
    assert fake.connections == 1  # 不重试：重放会让她把开头再念一遍
