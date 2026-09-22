"""WS Gateway：FRONTEND_DEMO_DESIGN.md §4 契约 ↔ EventBus 的序列化投影层。

权责（§4.4，纯增量模块——不改 EventBus/状态机/AgentReply/pipeline）：
- 上行：`WS /ws/chat` 的 JSON 文本帧 → 域事件注入 bus。`user.text` 走与
  语音轮次完全相同的事件序列（USER_SPEECH_STARTED → ASR_PARTIAL →
  ASR_FINAL → USER_SPEECH_STOPPED → TURN_COMPLETE），轮次裁决、打断、
  上下文记账全部仍由 ConversationCore 完成——网关不发明第二条路径。
- 下行：EventBus 事件 → §4.3 帧（state / asr.* / reply.* / interrupted /
  latency / error）。`reply.delta` 经 _SpeechDeltaTap 在 agent.stream_reply
  外包一层 SpeechFieldExtractor 实现，pipeline 本身零改动。
- 二进制帧 = PCM 16kHz/16bit/mono 上行音频（阶段 3）：user.audio.start/end
  之间收到的 bytes 直接喂进 pipeline 的音频源队列，VAD/SmartTurn/ASR 照常
  裁决；阶段 2 没有音频帧时该源静默等待，不影响文本轮次。
- `POST /chat`：§1.4 的可选 HTTP 冒烟旁路（文本进、整段 reply.final 出）。

运行：

    .venv/bin/python -m runtime.ws_gateway --mock      # 无 key 演示
    .venv/bin/python -m runtime.ws_gateway             # 真实 provider

配置：WS_GATEWAY_HOST / WS_GATEWAY_PORT / WS_GATEWAY_CORS_ORIGINS
（.env.example；CLI flag 优先）。前端 vite dev proxy 把 /ws/chat 转发到这里。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from character.agent import CharacterAgent
from conversation.core import ConversationCore
from conversation.events import Event, EventType, TEXT_TURN_SOURCE
from memory.loader import load_memory
from metrics.latency import LatencyLog
from providers.llm.base import LLMProvider
from runtime.pipeline import SpeechFieldExtractor, VoicePipeline
from runtime.simulated import (
    ScriptedASR,
    ScriptedLLM,
    ScriptedTurn,
    ScriptedVAD,
    ToneTTS,
    WavSinkPlayer,
)

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"
DEFAULT_OUTDIR = ROOT / "data" / "latency_log"

#: 下行投影订阅的事件面（其余域事件不外泄——契约即白名单）。
_PROJECTED_EVENTS = (
    EventType.STATE_CHANGED,
    EventType.ASR_PARTIAL,
    EventType.ASR_FINAL,
    EventType.AGENT_REPLY,
    EventType.AGENT_INTERRUPTED,
    EventType.PLAYBACK_STOPPED,
    EventType.PIPELINE_ERROR,
)



#: POST /chat 旁路等待 reply.final 的上限。
HTTP_REPLY_TIMEOUT_S = 30.0


# ---------------------------------------------------------------- 上行音频源


class WsAudioSource:
    """WS 二进制帧驱动的 pipeline 音频源；无帧时静默等待（不结束迭代）。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)

    def feed(self, pcm: bytes) -> None:
        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:
            pass  # 与 mic/asr 队列同一取舍：滞后丢帧保实时

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            yield await self._queue.get()


# ---------------------------------------------------------------- reply.delta 抽取


class _SpeechDeltaTap:
    """CharacterAgent 外观包装：stream_reply 的 token 流原样透传，旁路经
    SpeechFieldExtractor 解出的 speech 增量回调出去（→ reply.delta）。

    pipeline 只依赖 build_messages/stream_reply 两个方法；其余属性经
    __getattr__ 透传，未来 CharacterAgent 加方法也不漏。
    """

    def __init__(
        self, agent: CharacterAgent, on_delta: Callable[[str], None]
    ) -> None:
        object.__setattr__(self, "_agent", agent)
        object.__setattr__(self, "_on_delta", on_delta)

    def stream_reply(self, messages: Any, **kwargs: Any) -> AsyncIterator[str]:
        extractor = SpeechFieldExtractor()
        on_delta = self._on_delta
        inner = self._agent.stream_reply(messages, **kwargs)

        async def gen() -> AsyncIterator[str]:
            async for token in inner:
                piece = extractor.feed(token)
                if piece:
                    on_delta(piece)
                yield token

        return gen()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._agent, name)


# ---------------------------------------------------------------- audio.chunk 下行


class _AudioTapPlayer:
    """player 外观包装：write 先把 PCM 交给真实播放器，再原样广播给
    audio sinks（→ audio.chunk 头 + 二进制帧）。播放/打断语义全归 delegate，
    本层只做只读投影。"""

    def __init__(
        self,
        delegate: Any,
        sample_rate: int,
        emit: Callable[[bytes, str], None],
    ) -> None:
        self._delegate = delegate
        self._format = f"pcm{sample_rate / 1000:g}k"
        self._emit = emit

    def write(self, pcm: bytes) -> None:
        self._delegate.write(pcm)
        self._emit(bytes(pcm), self._format)

    @property
    def position_seconds(self) -> float:
        return self._delegate.position_seconds

    @property
    def pending_seconds(self) -> float:
        return self._delegate.pending_seconds

    @property
    def is_playing(self) -> bool:
        return self._delegate.is_playing

    def stop(self) -> float:
        return self._delegate.stop()

    def close(self) -> None:
        self._delegate.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


# ---------------------------------------------------------------- 事件 → 帧投影


def _latency_frame(metrics: LatencyLog, event: Event) -> dict | None:
    """PLAYBACK_STOPPED → latency 帧。此时 LatencyLog 已封账（订阅更早），
    按事件 ts 找回本轮 record；九时间戳口径见 metrics/latency.py。"""
    for rec in reversed(metrics.records):
        if rec.closed and rec.t_playback_stopped == event.ts:
            frame: dict[str, Any] = {"type": "latency"}
            if rec.sefa_ms is not None:
                frame["sefa_ms"] = round(rec.sefa_ms, 1)
            if rec.barge_in_stop_ms is not None:
                frame["barge_in_ms"] = round(rec.barge_in_stop_ms, 1)
            return frame if len(frame) > 1 else None
    return None


def project_event(
    event: Event, metrics: LatencyLog
) -> list[dict[str, Any]]:
    """EventBus 事件 → §4.3 帧列表（0..n）。字段名与契约逐字对齐。"""
    p = event.payload
    match event.type:
        case EventType.STATE_CHANGED:
            return [{"type": "state", "state": p.get("to")}]
        case EventType.ASR_PARTIAL:
            return [{"type": "asr.partial", "text": p.get("text", "")}]
        case EventType.ASR_FINAL:
            return [{"type": "asr.final", "text": p.get("text", "")}]
        case EventType.AGENT_REPLY:
            if p.get("error"):
                return [{"type": "error", "message": p["error"]}]
            return [
                {
                    "type": "reply.final",
                    "speech": p.get("speech", ""),
                    "emotion": p.get("emotion") or "neutral",
                    "energy": p.get("energy") or 0.0,
                }
            ]
        case EventType.AGENT_INTERRUPTED:
            return [{"type": "interrupted", "heard_text": p.get("heard", "")}]
        case EventType.PLAYBACK_STOPPED:
            frame = _latency_frame(metrics, event)
            return [frame] if frame else []
        case EventType.PIPELINE_ERROR:
            return [
                {
                    "type": "error",
                    "message": f"[{p.get('stage')}] {p.get('error')}",
                }
            ]
    return []


def inject_text_turn(bus, text: str) -> None:
    """user.text → 与语音轮次同构的域事件序列。

    文字输入视作"瞬时完成的语音轮次"：开轮 → 转写（partial+final）→
    停顿 → Smart Turn 裁决完成。轮次完成权仍归 TurnManager（C1），
    状态机迁移、barge-in 六步、latency 九时间戳全部照常发生。
    """
    bus.publish(EventType.USER_SPEECH_STARTED)
    bus.publish(EventType.ASR_PARTIAL, {"text": text})
    bus.publish(EventType.ASR_FINAL, {"text": text})
    bus.publish(EventType.USER_SPEECH_STOPPED)
    bus.publish(EventType.TURN_COMPLETE, {"source": TEXT_TURN_SOURCE})


# ---------------------------------------------------------------- 会话


class GatewayRuntime:
    """一个 gateway 进程共享的管线组装；每条 WS 连接挂一个会话投影。"""

    def __init__(self, args: argparse.Namespace, env: dict[str, str]) -> None:
        self.audio_source = WsAudioSource()
        vad, turn, asr, llm, tts = _build_providers(args, env)
        self.llm = llm
        self._audio_sinks: set[Callable[[bytes, str], None]] = set()
        local_player = _build_player(args, tts)
        self.player = _AudioTapPlayer(
            local_player, tts.sample_rate, self._emit_audio
        )
        agent = CharacterAgent(llm)
        self._delta_sinks: set[Callable[[str], None]] = set()
        tapped = _SpeechDeltaTap(agent, self._emit_delta)
        memory_dir = env.get("MEMORY_DIR") or (ROOT / "memory")
        memory_pack = load_memory(memory_dir)
        # MEMORY_PROVIDER=mem0 → 向量检索档（记忆槽位由召回结果填充，
        # fixture 内容 seed 进 mem0）；否则 fixture 摘要常驻 system prompt。
        memory_provider = None
        memory_text = memory_pack.text
        if (env.get("MEMORY_PROVIDER") or "").lower() == "mem0":
            from memory.mem0_provider import Mem0MemoryProvider

            memory_provider = Mem0MemoryProvider(
                api_key=env["DASHSCOPE_API_KEY"],
                base_url=env.get("OPENAI_COMPATIBLE_URL")
                or "https://dashscope.aliyuncs.com/compatible-mode/v1",
                llm_model=env.get("QWEN_MODEL") or "qwen-flash",
                embed_model=env.get("MEM0_EMBED_MODEL") or "text-embedding-v3",
                persist_dir=env.get("MEM0_DIR") or (ROOT / "data" / "mem0"),
            )
            memory_provider.seed_fixture(memory_dir)
            memory_text = ""
        self.core = ConversationCore(
            playback=self.player,
            tts=tts,
            min_barge_in_s=args.min_barge_in_s,
            memory_text=memory_text,
            memory_provider=memory_provider,
        )
        self.metrics = LatencyLog(self.core.bus, outdir=args.outdir)
        self.pipeline = VoicePipeline(
            audio=self.audio_source,
            vad=vad,
            turn=turn,
            asr=asr,
            agent=tapped,
            tts=tts,
            player=self.player,
            core=self.core,
            metrics=self.metrics,
        )
        self._pipeline_task: asyncio.Task | None = None

    @property
    def bus(self):
        return self.core.bus

    async def start(self) -> None:
        self._pipeline_task = asyncio.create_task(
            self.pipeline.run(), name="voice-pipeline"
        )

    async def stop(self) -> None:
        self.pipeline.stop()
        if self._pipeline_task is not None:
            await asyncio.gather(self._pipeline_task, return_exceptions=True)
        close = getattr(self.llm, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass

    # ---- 会话挂载 ----

    def add_delta_sink(self, sink: Callable[[str], None]) -> None:
        self._delta_sinks.add(sink)

    def remove_delta_sink(self, sink: Callable[[str], None]) -> None:
        self._delta_sinks.discard(sink)

    def _emit_delta(self, piece: str) -> None:
        for sink in list(self._delta_sinks):
            sink(piece)

    def add_audio_sink(self, sink: Callable[[bytes, str], None]) -> None:
        self._audio_sinks.add(sink)

    def remove_audio_sink(self, sink: Callable[[bytes, str], None]) -> None:
        self._audio_sinks.discard(sink)

    def _emit_audio(self, pcm: bytes, format: str) -> None:
        for sink in list(self._audio_sinks):
            sink(pcm, format)


class ChatSession:
    """一条 /ws/chat 连接 = 一个会话（§4.1）。双向循环：

    - 收：JSON 文本帧按 §4.2 注入；二进制帧在 audio.start/end 窗口内上行；
      未知 type 忽略（与前端同一条向前兼容纪律）；非法 JSON 回 error 帧。
    - 发：bus 事件经 project_event 投影进 outbox，sender 协程逐帧写 WS。
    """

    def __init__(self, ws: WebSocket, rt: GatewayRuntime) -> None:
        self._ws = ws
        self._rt = rt
        self._outbox: asyncio.Queue[dict[str, Any] | bytes] = asyncio.Queue()
        self._subs: list = []
        self._capturing_audio = False
        self._audio_seq = 0

    async def run(self) -> None:
        await self._ws.accept()
        for et in _PROJECTED_EVENTS:
            self._subs.append(self._rt.bus.subscribe(et, self._on_event))
        self._rt.add_delta_sink(self._on_delta)
        self._rt.add_audio_sink(self._on_audio)
        sender = asyncio.create_task(self._send_loop(), name="ws-send")
        try:
            while True:
                message = await self._ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    if self._capturing_audio:
                        self._rt.audio_source.feed(message["bytes"])
                    continue
                text = message.get("text")
                if text is not None and self._on_text_frame(text):
                    break  # session.end
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            for et, h in zip(_PROJECTED_EVENTS, self._subs):
                self._rt.bus.unsubscribe(et, h)
            self._rt.remove_delta_sink(self._on_delta)
            self._rt.remove_audio_sink(self._on_audio)

    # ---- bus → ws ----

    def _on_event(self, event: Event) -> None:
        for frame in project_event(event, self._rt.metrics):
            self._outbox.put_nowait(frame)

    def _on_delta(self, piece: str) -> None:
        self._outbox.put_nowait({"type": "reply.delta", "text": piece})

    def _on_audio(self, pcm: bytes, format: str) -> None:
        """§4.3 audio.chunk：头帧紧跟二进制负载，seq 逐块递增。"""
        self._audio_seq += 1
        self._outbox.put_nowait(
            {"type": "audio.chunk", "seq": self._audio_seq, "format": format}
        )
        self._outbox.put_nowait(pcm)

    async def _send_loop(self) -> None:
        while True:
            frame = await self._outbox.get()
            if isinstance(frame, bytes):
                await self._ws.send_bytes(frame)
            else:
                await self._ws.send_text(
                    json.dumps(frame, ensure_ascii=False)
                )

    # ---- ws → bus ----

    def _on_text_frame(self, text: str) -> bool:
        """处理一帧 §4.2 消息；返回 True 表示客户端要求关闭（session.end）。"""
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            self._outbox.put_nowait(
                {"type": "error", "message": "malformed JSON frame"}
            )
            return False
        if not isinstance(frame, dict):
            return False
        match frame.get("type"):
            case "session.start":
                self._outbox.put_nowait(
                    {"type": "state", "state": str(self._rt.core.state)}
                )
            case "user.text":
                text_in = (frame.get("text") or "").strip()
                if text_in:
                    inject_text_turn(self._rt.bus, text_in)
            case "user.audio.start":
                self._capturing_audio = True
            case "user.audio.end":
                self._capturing_audio = False
            case "session.end":
                return True
            case _:
                pass  # 未知 type：忽略（§4.2 向前兼容）
        return False


# ---------------------------------------------------------------- HTTP 冒烟旁路


async def post_chat(request: Request) -> JSONResponse:
    """§1.4 调试旁路：{"text": "…"} → 整段 reply.final（不流经 WS）。"""
    rt: GatewayRuntime = request.app.state.runtime
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "malformed JSON"}, status_code=400)
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict] = loop.create_future()

    def on_reply(event: Event) -> None:
        if not future.done():
            future.set_result(
                {
                    "speech": event.payload.get("speech", ""),
                    "emotion": event.payload.get("emotion") or "neutral",
                    "energy": event.payload.get("energy") or 0.0,
                }
            )

    rt.bus.subscribe(EventType.AGENT_REPLY, on_reply)
    try:
        inject_text_turn(rt.bus, text)
        reply = await asyncio.wait_for(future, HTTP_REPLY_TIMEOUT_S)
    except asyncio.TimeoutError:
        return JSONResponse({"error": "reply timeout"}, status_code=504)
    finally:
        rt.bus.unsubscribe(EventType.AGENT_REPLY, on_reply)
    return JSONResponse(reply)


# ---------------------------------------------------------------- provider 组装


def _build_providers(args, env):
    """mock：全脚本化（无 key 演示）；real：与 runtime.main._build_real 同选型，
    唯音频源换成 WS 上行队列（麦克风在浏览器端，阶段 3）。"""
    if args.mock:

        def mock_reply(messages):
            # 最后一条 user 消息是 render_turn_input 的键值块，取 user: 行。
            user_text = ""
            for m in reversed(list(messages)):
                if getattr(m, "get", None) and m.get("role") == "user":
                    content = str(m.get("content") or "")
                    for line in reversed(content.splitlines()):
                        if line.startswith('user: "'):
                            user_text = line[7:-1]
                            break
                    break
            short = user_text[:20] or "……"
            return {
                "speech": f"听到了听到了！你说「{short}」对吧？派蒙全都听见啦。",
                "emotion": "excited",
                "energy": 0.8,
                "should_continue": False,
            }

        return (
            ScriptedVAD([]),
            ScriptedTurn(),
            ScriptedASR(["（阶段 3 才有音频上行）"]),
            ScriptedLLM(mock_reply, token_size=4, token_delay_s=0.01),
            ToneTTS(sample_rate=24000, secs_per_chunk=0.15),
        )

    from providers.asr import DashScopeASR
    from providers.llm import OpenAICompatibleLLM
    from providers.tts import BailianCosyVoiceTTS, FishAudioTTS
    from turn.smart_turn_adapter import SmartTurnAdapter
    from turn.vad_adapter import SileroVADAdapter

    dash_key = env.get("DASHSCOPE_API_KEY", "")
    fish_key = env.get("FISH_API_KEY", "")
    if not dash_key:
        raise SystemExit(
            "缺少 DASHSCOPE_API_KEY（ASR + 备用 TTS + 百炼 LLM）。"
            "填 .env 或用 --mock 演示。"
        )
    llm_base = env.get(
        "OPENAI_COMPATIBLE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    llm_model = args.llm_model or env.get("QWEN_MODEL", "qwen-flash")
    ws_url = env.get("DASHSCOPE_WS_URL")
    vad = SileroVADAdapter()
    turn = SmartTurnAdapter(wait_for_transcript=True)
    asr = DashScopeASR(api_key=dash_key, **({"url": ws_url} if ws_url else {}))
    llm = OpenAICompatibleLLM(base_url=llm_base, api_key=dash_key, model=llm_model)
    # TTS_PROVIDER 显式选型：默认 bailian——Fish 跨境握手在本网络环境
    # 实测持续超时（D-014 赛马结果）；海外/VPS 环境可设 TTS_PROVIDER=fish。
    tts_kind = env.get("TTS_PROVIDER", "bailian").lower()
    if tts_kind == "fish":
        if not fish_key:
            raise SystemExit("TTS_PROVIDER=fish 但缺少 FISH_API_KEY。")
        tts = FishAudioTTS(api_key=fish_key)
    else:
        tts = BailianCosyVoiceTTS(
            api_key=dash_key,
            **({"url": ws_url} if ws_url else {}),
            **({"voice": env["TTS_VOICE"]} if env.get("TTS_VOICE") else {}),
        )
    return vad, turn, asr, llm, tts


def _build_player(args, tts):
    """gateway 默认浏览器-only 放音：PCM 经 audio.chunk 下行到前端，
    本机只落 wav 审计（避免浏览器 + Python 扬声器双播）。
    --local-playback 才走声卡外放（无声卡仍落 wav）。"""
    if getattr(args, "local_playback", False):
        try:
            import sounddevice as sd

            default_out = sd.default.device[1]
            if default_out is None or int(default_out) < 0:
                raise RuntimeError("no default output device")
            from runtime.playback import StreamingPlayer

            return StreamingPlayer(sample_rate=tts.sample_rate)
        except Exception as e:
            print(
                f"[playback] no output device ({e}) → WavSinkPlayer 落盘",
                file=sys.stderr,
            )
    wav = args.playback_wav or ROOT / "data" / "playback_ws.wav"
    return WavSinkPlayer(
        sample_rate=tts.sample_rate, out_path=wav, realtime=True
    )


# ---------------------------------------------------------------- app / 入口


def _load_env() -> dict[str, str]:
    import os

    env = {k: v for k, v in dotenv_values(ENV_FILE).items() if v is not None}
    for k in (
        "DASHSCOPE_API_KEY",
        "FISH_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_COMPATIBLE_URL",
        "DASHSCOPE_WS_URL",
        "QWEN_MODEL",
        "WS_GATEWAY_HOST",
        "WS_GATEWAY_PORT",
        "WS_GATEWAY_CORS_ORIGINS",
        "TTS_PROVIDER",
        "TTS_VOICE",
        "MEMORY_DIR",
    ):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def create_app(args: argparse.Namespace, env: dict[str, str]) -> Starlette:
    """组装 Starlette app；pipeline 生命周期挂进 ASGI lifespan。"""

    @asynccontextmanager
    async def lifespan(app: Starlette):
        rt = GatewayRuntime(args, env)
        app.state.runtime = rt
        await rt.start()
        yield
        await rt.stop()

    routes = [
        WebSocketRoute("/ws/chat", _ws_chat),
        Route("/chat", post_chat, methods=["POST"]),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    origins = [
        o.strip()
        for o in (env.get("WS_GATEWAY_CORS_ORIGINS") or "").split(",")
        if o.strip()
    ]
    if origins:
        from starlette.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware, allow_origins=origins, allow_methods=["POST"]
        )
    return app


async def _ws_chat(ws: WebSocket) -> None:
    rt: GatewayRuntime = ws.app.state.runtime
    await ChatSession(ws, rt).run()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m runtime.ws_gateway",
        description="Paimon WS gateway — FRONTEND_DEMO_DESIGN.md §4 contract (TASK-017)",
    )
    p.add_argument("--host", help="覆盖 WS_GATEWAY_HOST（默认 127.0.0.1）")
    p.add_argument("--port", type=int, help="覆盖 WS_GATEWAY_PORT（默认 8765）")
    p.add_argument(
        "--mock", action="store_true", help="ASR/LLM/TTS 用脚本化实现"
    )
    p.add_argument("--llm-model", help="覆盖 LLM model 名")
    p.add_argument(
        "--min-barge-in-s", type=float, default=0.0, help="最短打断时长门槛"
    )
    p.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    p.add_argument(
        "--playback-wav", type=Path, help="无声卡时 WavSinkPlayer 落盘路径"
    )
    p.add_argument(
        "--local-playback",
        action="store_true",
        help="同时在后端机器声卡外放（默认关：浏览器已有 audio.chunk 下行，"
        "避免双播）",
    )
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = _parse_args()
    env = _load_env()
    host = args.host or env.get("WS_GATEWAY_HOST") or "127.0.0.1"
    port = args.port or int(env.get("WS_GATEWAY_PORT") or 8765)
    if not args.verbose:
        try:
            from loguru import logger

            logger.remove()
            logger.add(sys.stderr, level="WARNING")
        except Exception:
            pass
    app = create_app(args, env)
    print(
        f"[ws_gateway] listening on ws://{host}:{port}/ws/chat"
        + (" (mock providers)" if args.mock else ""),
        file=sys.stderr,
        flush=True,
    )
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
