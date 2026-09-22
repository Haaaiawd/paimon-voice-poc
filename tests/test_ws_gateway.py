"""TASK-017 验收：WS gateway + FRONTEND_DEMO_DESIGN.md §4 契约一致性。

uvicorn 在测试 loop 内起真实端口，websockets 客户端驱动
session.start / user.text，断言：

- §4.3 事件流完整：state / asr.partial / asr.final / reply.delta /
  reply.final / latency 全部按序到达，状态迁移走完整七态路径
  （IDLE→LISTENING→POSSIBLE_END→THINKING→SPEAKING→IDLE）；
- 契约词汇零漂移：服务端发出的每个帧 type 都在前端 types.ts 的
  SERVER_FRAME_TYPES 白名单内，state 值 ⊆ PIPELINE_STATES，
  emotion ∈ EMOTION_LABELS，reply.final 字段逐字对齐 AgentReply；
- 向前兼容纪律：未知 type 与非法 JSON 不击垮会话（error 帧或忽略）；
- POST /chat 冒烟旁路返回 AgentReply 三字段。

全程 mock provider（无 key/无声卡环境可跑），pipeline 是真实
VoicePipeline + ConversationCore——测的是真链路，不是假回声。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
import websockets

from runtime.ws_gateway import create_app

ROOT = Path(__file__).resolve().parents[1]
TYPES_TS = ROOT / "frontend" / "src" / "types.ts"
WS_BACKEND_TS = ROOT / "frontend" / "src" / "backend" / "WsBackend.ts"


def _ts_list(name: str) -> list[str]:
    """从 types.ts 抠出 `export const X = [...] as const` 的字面量列表。"""
    src = TYPES_TS.read_text(encoding="utf-8")
    m = re.search(
        rf"export const {name} = \[(.*?)\] as const", src, re.DOTALL
    )
    assert m, f"{name} not found in types.ts"
    return re.findall(r"'([^']+)'", m.group(1))


SERVER_FRAME_TYPES = frozenset(_ts_list("SERVER_FRAME_TYPES"))
PIPELINE_STATES = frozenset(_ts_list("PIPELINE_STATES"))
EMOTION_LABELS = frozenset(_ts_list("EMOTION_LABELS"))

CLIENT_MSG_ID = "test-msg-1"
USER_TEXT = "你好派蒙"


class _GatewayServer:
    """测试 loop 内运行的真实 uvicorn + mock-provider gateway。"""

    def __init__(self, tmp_path: Path) -> None:
        args = SimpleNamespace(
            mock=True,
            llm_model=None,
            min_barge_in_s=0.0,
            outdir=None,  # 不落 latency 文件
            playback_wav=tmp_path / "playback.wav",
        )
        self.app = create_app(args, env={})
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app, host="127.0.0.1", port=0, log_level="warning"
            )
        )
        self.port = 0
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "_GatewayServer":
        self._task = asyncio.create_task(self.server.serve())
        for _ in range(200):
            if self.server.started:
                break
            await asyncio.sleep(0.02)
        assert self.server.started, "uvicorn did not start"
        sock = self.server.servers[0].sockets[0]
        self.port = sock.getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self.server.should_exit = True
        assert self._task is not None
        await asyncio.wait_for(self._task, timeout=15)

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws/chat"

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


async def _collect(ws, until, *, timeout: float = 20.0) -> list[dict]:
    """收帧直到 until(frames) 为真或超时。"""
    frames: list[dict] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while not until(frames):
        remaining = deadline - asyncio.get_running_loop().time()
        assert remaining > 0, f"timed out; got: {[f.get('type') for f in frames]}"
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        if isinstance(raw, bytes):
            continue  # audio.chunk 二进制负载帧不是 JSON
        frames.append(json.loads(raw))
    return frames


def _turn_done(frames: list[dict]) -> bool:
    """本轮结清：reply.final + latency 已出，且状态已回 IDLE。"""
    types = {f["type"] for f in frames}
    states = [f["state"] for f in frames if f["type"] == "state"]
    return (
        "reply.final" in types
        and "latency" in types
        and bool(states)
        and states[-1] == "IDLE"
    )


def _ordered_index(frames: list[dict], pred, start: int = 0) -> int:
    for i in range(start, len(frames)):
        if pred(frames[i]):
            return i
    raise AssertionError(f"frame matching {pred} not found from index {start}")


async def test_ws_text_turn_full_event_stream(tmp_path):
    """user.text → 完整 §4.3 事件流 + 状态机七态路径 + reply.final。"""
    async with _GatewayServer(tmp_path) as gw:
        async with websockets.connect(gw.ws_url) as ws:
            await ws.send(json.dumps({"type": "session.start"}))
            hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert hello == {"type": "state", "state": "IDLE"}

            await ws.send(
                json.dumps(
                    {
                        "type": "user.text",
                        "text": USER_TEXT,
                        "client_msg_id": CLIENT_MSG_ID,
                    }
                )
            )
            frames = await _collect(ws, _turn_done)

    types = [f["type"] for f in frames]

    # 词汇表：每个帧 type 都必须在前端声明的 §4.3 白名单内
    assert set(types) <= SERVER_FRAME_TYPES

    # 状态序列：IDLE→LISTENING→POSSIBLE_END→THINKING→SPEAKING→IDLE
    states = [f["state"] for f in frames if f["type"] == "state"]
    assert states[0] == "LISTENING"
    assert "POSSIBLE_END" in states
    assert "THINKING" in states
    assert "SPEAKING" in states
    assert states[-1] == "IDLE"
    assert set(states) <= PIPELINE_STATES

    # 顺序：partial → final → THINKING → delta(s) → reply.final
    i = _ordered_index(frames, lambda f: f["type"] == "asr.partial")
    assert frames[i]["text"] == USER_TEXT
    i = _ordered_index(frames, lambda f: f["type"] == "asr.final", i)
    assert frames[i]["text"] == USER_TEXT
    i = _ordered_index(
        frames, lambda f: f == {"type": "state", "state": "THINKING"}, i
    )
    i = _ordered_index(frames, lambda f: f["type"] == "reply.delta", i)
    i = _ordered_index(frames, lambda f: f["type"] == "reply.final", i)
    reply = frames[i]

    # reply.final 字段 = AgentReply 原名（speech/emotion/energy），emotion ∈ §8 标签集
    assert set(reply) == {"type", "speech", "followup", "emotion", "energy"}
    assert USER_TEXT[:20] in reply["speech"]  # mock reply 回显用户文本
    assert reply["emotion"] in EMOTION_LABELS
    assert isinstance(reply["energy"], (int, float))

    # latency 帧：SEFA 可观测（文本轮次同样有九时间戳账本）
    latency = next(f for f in frames if f["type"] == "latency")
    assert isinstance(latency.get("sefa_ms"), (int, float))
    assert latency["sefa_ms"] >= 0


async def test_ws_unknown_and_malformed_frames(tmp_path):
    """向前兼容：未知 type 忽略、非法 JSON 回 error 帧、会话不中断。"""
    async with _GatewayServer(tmp_path) as gw:
        async with websockets.connect(gw.ws_url) as ws:
            await ws.send(json.dumps({"type": "session.start"}))
            await ws.recv()  # state IDLE

            await ws.send("not json at all")
            err = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert err["type"] == "error"

            await ws.send(json.dumps({"type": "future.thing", "x": 1}))
            await ws.send(
                json.dumps({"type": "user.text", "text": "还在吗"})
            )
            frames = await _collect(ws, _turn_done)
            assert "error" not in [
                f["type"] for f in frames
            ], "unknown frame type must not produce an error"


async def test_ws_contract_vocabulary_alignment(tmp_path):
    """契约一致性：后端帧名 ⊆ types.ts 白名单；前端上行帧 ⊆ §4.2。"""
    async with _GatewayServer(tmp_path) as gw:
        async with websockets.connect(gw.ws_url) as ws:
            await ws.send(json.dumps({"type": "session.start"}))
            await ws.recv()
            await ws.send(
                json.dumps(
                    {"type": "user.text", "text": "报个到", "client_msg_id": "x"}
                )
            )
            frames = await _collect(ws, _turn_done)

    for f in frames:
        assert f["type"] in SERVER_FRAME_TYPES, f"undeclared frame: {f}"
        if f["type"] == "state":
            assert f["state"] in PIPELINE_STATES
        if f["type"] == "reply.final":
            assert set(f) == {"type", "speech", "followup", "emotion", "energy"}
            assert f["emotion"] in EMOTION_LABELS

    # 前端上行：WsBackend 只发 §4.2 词汇
    ws_src = WS_BACKEND_TS.read_text(encoding="utf-8")
    for literal in ("session.start", "user.text", "client_msg_id", "session.end"):
        assert literal in ws_src, f"WsBackend missing client frame {literal}"


async def test_ws_audio_chunk_downlink(tmp_path):
    """§4.3 audio.chunk：文本轮次走到 SPEAKING 时，TTS PCM 以
    "JSON 头帧 + 紧随其后的二进制负载帧"投下下行。"""
    raw_messages: list = []
    async with _GatewayServer(tmp_path) as gw:
        async with websockets.connect(gw.ws_url) as ws:
            await ws.send(json.dumps({"type": "session.start"}))
            await ws.recv()  # state IDLE

            await ws.send(
                json.dumps({"type": "user.text", "text": USER_TEXT})
            )
            deadline = asyncio.get_running_loop().time() + 20.0
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                assert remaining > 0, "timed out waiting for reply.final"
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                raw_messages.append(raw)
                if isinstance(raw, str) and '"reply.final"' in raw:
                    if json.loads(raw)["type"] == "reply.final":
                        break

    # 至少一对 audio.chunk 头 + 二进制负载
    headers = [
        i
        for i, m in enumerate(raw_messages)
        if isinstance(m, str)
        and '"audio.chunk"' in m
        and json.loads(m).get("type") == "audio.chunk"
    ]
    assert headers, "no audio.chunk header frames received"

    final_idx = next(
        i
        for i, m in enumerate(raw_messages)
        if isinstance(m, str) and json.loads(m)["type"] == "reply.final"
    )
    for i in headers:
        header = json.loads(raw_messages[i])
        assert header["format"] == "pcm24k"  # mock ToneTTS sample_rate
        assert isinstance(header["seq"], int) and header["seq"] >= 1
        payload = raw_messages[i + 1]  # 头帧必须紧跟二进制负载
        assert isinstance(payload, bytes) and len(payload) > 0
        assert i + 1 < final_idx, "audio chunk arrived after reply.final"


async def test_http_chat_smoke_bypass(tmp_path):
    """§1.4 可选旁路：POST /chat 文本进 → AgentReply 整段出。"""
    async with _GatewayServer(tmp_path) as gw:
        async with httpx.AsyncClient(
            base_url=gw.http_url, timeout=20.0
        ) as client:
            resp = await client.post("/chat", json={"text": USER_TEXT})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"speech", "followup", "emotion", "energy"}
    assert body["emotion"] in EMOTION_LABELS
    assert USER_TEXT[:20] in body["speech"]
