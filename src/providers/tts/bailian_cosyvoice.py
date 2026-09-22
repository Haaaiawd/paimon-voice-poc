"""百炼 CosyVoice 流式 TTS adapter：cosyvoice-v3-flash，DashScope WS duplex。

协议（与 dashscope SDK SpeechSynthesizer(tts_v2) 相同 wire format，自研 async
实现，和 DashScopeASR 同端点同信封）：
  connect(Authorization: Bearer) → run-task(model, parameters) → task-started
    → continue-task(payload.input.text)×N → 服务端二进制 PCM 帧下行
    → finish-task → task-finished → 连接保持，下一个 task 复用。

官方建议 WS 常驻复用（每轮新 task_id）——正是热 TTFA 口径：连接在
stream_audio 之前已建好，计时不含 TCP/TLS 握手。

cancel 契约（base.py docstring 三条）实现：
- 停推：发 finish-task(directive=cancel) 通知服务端中止，本地即刻不再
  产出音频（cancelled 标记下二进制帧只丢弃不上抛）；
- buffer 清理：取消任务的残余帧在 task-finished 前全部排空丢弃，
  不留到下一任务串音；
- 可重开：cancel() 把残余帧排完（有界 cancel_timeout）才返回，返回即
  连接干净可直接跑新任务；超时则关 socket，下次自动重连。

读者唯一性：socket 的 recv 任何时刻只有一个执行者——生成器在主循环/
_handshake 中持 in_recv 标记；当它停在 yield（barge-in 同任务调用 cancel
的典型形态）时 cancel() 亲自排空。state.draining 防生成器收尾与 cancel
排空撞车。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from .base import TTSError, TTSProvider

DEFAULT_MODEL = "cosyvoice-v3-flash"  # v3.5-flash 本账号 418，勿用
DEFAULT_WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
# chinese-tts-eval C5：longhuhu_v3 天真烂漫女童，最贴派蒙（实测首音 0.83s
# 快于 longanhuan_v3）；支持 Instruct 情感指令。TTS_VOICE 环境变量可换。
DEFAULT_VOICE = "longhuhu_v3"

# dashscope SDK Request.get_start_request 同款 parameters 面。
DEFAULT_PARAMETERS: Mapping[str, Any] = {
    "volume": 50,
    "text_type": "PlainText",
    "rate": 1.0,
    "pitch": 1.0,
    "seed": 0,
    "type": 0,
}


def _envelope(action: str, task_id: str, payload: Mapping[str, Any]) -> str:
    return json.dumps(
        {
            "header": {
                "action": action,
                "task_id": task_id,
                "streaming": "duplex",
            },
            "payload": payload,
        },
        ensure_ascii=False,
    )


@dataclass
class _Task:
    """一次合成的运行态；cancel/teardown 经它协调，保证 socket 干净复用。"""

    task_id: str
    cancelled: bool = False
    finish_sent: bool = False  # finish-task（正常或 cancel）已发出
    started: bool = False
    in_recv: bool = False  # 生成器正阻塞在 ws.recv()
    draining: bool = False  # cancel() 正在排空
    done: asyncio.Event = field(default_factory=asyncio.Event)
    error: Exception | None = None
    pump: asyncio.Task[None] | None = None


class BailianCosyVoiceTTS(TTSProvider):
    """cosyvoice-v3-flash 流式合成，WS 常驻连接跨任务复用。

    `stream_audio(chunks)` 每个语义边界 chunk 映射为一条 continue-task，
    chunks 结束发 finish-task 收尾；产出 format 指定的音频字节
    （默认 24000Hz int16 mono PCM，`sample_rate` 属性供播放侧开流）。
    `instruction` 透传 v3 系统音色的 Instruct 文本指令
    （如 "你说话的情感是happy。"），emotion 映射由上游组装。
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        url: str = DEFAULT_WS_URL,
        voice: str = DEFAULT_VOICE,
        audio_format: str = "pcm",
        sample_rate: int = 24000,
        instruction: str | None = None,
        parameters: Mapping[str, Any] | None = None,
        open_timeout: float = 15.0,
        cancel_timeout: float = 5.0,
        proxy: str | None = None,
        connector: Any | None = None,
    ) -> None:
        if not api_key:
            raise TTSError("BailianCosyVoiceTTS: api_key is required")
        self._api_key = api_key
        self.model = model
        self.url = url
        self.voice = voice
        self.audio_format = audio_format
        self.sample_rate = sample_rate
        self.instruction = instruction
        self.parameters = {**DEFAULT_PARAMETERS, **(parameters or {})}
        self.open_timeout = open_timeout
        self.cancel_timeout = cancel_timeout
        # proxy=None → websockets 直连，绕开 env 里的 HTTP(S)_PROXY（Clash 坑）
        self.proxy = proxy
        # 测试可注入替代 connector（签名同 websockets.connect）
        self._connect = connector or websockets.connect

        self._ws: ClientConnection | None = None
        self._active: _Task | None = None
        self._closed = False
        # 延迟观测：run-task 发出 → 首字节音频的毫秒数（demo/bench 用）
        self.last_ttfa_ms: float | None = None
        # 本轮是否复用了常驻连接（热路径证据，bench 用）
        self.last_acquire_warm: bool = False

    # ---- 协议帧 ----

    def _run_task_message(self, task_id: str) -> str:
        parameters = {
            **self.parameters,
            "voice": self.voice,
            "sample_rate": self.sample_rate,
            "format": self.audio_format,
        }
        if self.instruction is not None:
            parameters["instruction"] = self.instruction
        return _envelope(
            "run-task",
            task_id,
            {
                "task_group": "audio",
                "task": "tts",
                "function": "SpeechSynthesizer",
                "model": self.model,
                "parameters": parameters,
                "input": {},
            },
        )

    def _continue_task_message(self, task_id: str, text: str) -> str:
        return _envelope(
            "continue-task",
            task_id,
            {
                "task_group": "audio",
                "task": "tts",
                "function": "SpeechSynthesizer",
                "model": self.model,
                "input": {"text": text},
            },
        )

    def _finish_task_message(
        self, task_id: str, directive: str | None = None
    ) -> str:
        inp: dict[str, Any] = {}
        if directive is not None:
            inp["directive"] = directive
        return _envelope("finish-task", task_id, {"input": inp})

    # ---- 连接管理 ----

    async def _ensure_ws(self) -> ClientConnection:
        """返回常驻连接；无连接/已断开则新建。连接级失败包成 TTSError。"""
        ws = self._ws
        if ws is not None:
            try:
                pong = await ws.ping()  # 廉价活性检查；死了就重连
                await asyncio.wait_for(pong, timeout=3.0)
                self.last_acquire_warm = True
                return ws
            except Exception:
                await self._drop_ws()
        self.last_acquire_warm = False
        try:
            self._ws = await self._connect(
                self.url,
                additional_headers={"Authorization": f"Bearer {self._api_key}"},
                open_timeout=self.open_timeout,
                proxy=self.proxy,
            )
        except Exception as e:
            raise TTSError(f"BailianCosyVoiceTTS: connect failed: {e}") from e
        return self._ws

    async def _drop_ws(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def _send_cancel_finish(self, ws: ClientConnection, state: _Task) -> bool:
        """补发 finish-task(directive=cancel)；失败则整线断开。返回连接是否可用。"""
        if state.finish_sent:
            return True
        try:
            await ws.send(self._finish_task_message(state.task_id, "cancel"))
            state.finish_sent = True
            return True
        except Exception:
            await self._drop_ws()
            state.done.set()
            return False

    async def _drain_task(self, ws: ClientConnection, state: _Task) -> None:
        """排空到 task-finished；超时则整线断开。调用方须保证自己是唯一读者。"""
        if not await self._send_cancel_finish(ws, state):
            return
        try:
            while not state.done.is_set():
                raw = await asyncio.wait_for(
                    ws.recv(), timeout=self.cancel_timeout
                )
                if isinstance(raw, str):
                    self._route_event(raw, state)
                # 二进制残余帧丢弃——不进下次合成（cancel 契约 2）
        except (TimeoutError, websockets.ConnectionClosed):
            await self._drop_ws()
            state.done.set()

    # ---- TTSProvider ----

    async def stream_audio(
        self,
        chunks: AsyncIterable[str],
        **kwargs: Any,
    ) -> AsyncIterator[bytes]:
        if self._closed:
            raise TTSError("BailianCosyVoiceTTS is closed")
        if self._active is not None:
            # 上一任务未正常收尾（消费方 abandon 且未 cancel）：
            # 与其串音不如整线重置
            await self._drop_ws()
            self._active = None

        state = _Task(task_id=uuid.uuid4().hex)
        self._active = state
        self.last_ttfa_ms = None
        t0 = time.monotonic()
        ws = await self._ensure_ws()

        async def pump() -> None:
            try:
                async for text in chunks:
                    if not text:
                        continue
                    await ws.send(self._continue_task_message(state.task_id, text))
                await ws.send(self._finish_task_message(state.task_id))
                state.finish_sent = True
            except websockets.ConnectionClosed:
                pass  # 接收侧以协议事件/错误收尾
            except Exception as e:
                if state.error is None:
                    state.error = TTSError(f"text source failed: {e}")
                await self._drop_ws()  # 文本源挂了 → 让接收侧退出

        try:
            await ws.send(self._run_task_message(state.task_id))
            state.in_recv = True
            await self._wait_started(ws, state)
            state.in_recv = False

            state.pump = asyncio.create_task(pump())
            while not state.done.is_set():
                state.in_recv = True
                raw = await ws.recv()  # cancel() 不碰 recv，见 docstring
                state.in_recv = False
                if isinstance(raw, (bytes, bytearray)):
                    if state.cancelled:
                        continue  # 残余帧排空丢弃，不上抛（cancel 契约 1/2）
                    if self.last_ttfa_ms is None:
                        self.last_ttfa_ms = (time.monotonic() - t0) * 1000
                    yield bytes(raw)
                else:
                    self._route_event(raw, state)
            if state.error is not None:
                raise state.error
        except websockets.ConnectionClosed as e:
            if state.error is not None:
                raise state.error from e
            if state.done.is_set() or state.cancelled:
                pass  # 正常收尾 / cancel 超时强断
            else:
                await self._drop_ws()
                raise TTSError(
                    f"connection closed before task-finished: {e}"
                ) from e
        finally:
            state.in_recv = False
            if state.pump is not None and not state.pump.done():
                state.pump.cancel()
                await asyncio.gather(state.pump, return_exceptions=True)
            if not state.done.is_set() and not state.draining:
                # 消费方提前退出（GeneratorExit）且无人排空：
                # 原地排空到 task-finished，保 socket 干净可复用
                state.cancelled = True
                await self._drain_task(ws, state)
            self._active = None
        if state.error is not None:
            raise state.error  # pump 侧错误（文本源挂了）透传

    async def cancel(self) -> None:
        """打断当前合成：发 cancel 指令 + 排空残余帧 + 保连接复用。幂等。"""
        state = self._active
        if state is None or state.done.is_set():
            return
        state.cancelled = True
        if state.pump is not None and not state.pump.done():
            state.pump.cancel()
        ws = self._ws
        if ws is None:
            state.done.set()
            return
        if not await self._send_cancel_finish(ws, state):
            return
        if state.in_recv:
            # 生成器在读：它会把残余帧排空并见到 task-finished
            try:
                await asyncio.wait_for(
                    state.done.wait(), timeout=self.cancel_timeout
                )
            except TimeoutError:
                await self._drop_ws()
                state.done.set()
        else:
            # 生成器停在 yield（barge-in 同任务形态）：cancel 亲自排空
            state.draining = True
            try:
                await self._drain_task(ws, state)
            finally:
                state.draining = False

    async def close(self) -> None:
        """关闭常驻连接；幂等。"""
        self._closed = True
        state = self._active
        if state is not None:
            state.cancelled = True
            state.done.set()
        await self._drop_ws()

    # ---- 内部 ----

    async def _wait_started(self, ws: ClientConnection, state: _Task) -> None:
        """run-task 后等到 task-started；task-failed/超时抛 TTSError。"""
        try:
            while not state.done.is_set():
                raw = await asyncio.wait_for(
                    ws.recv(), timeout=self.open_timeout
                )
                if isinstance(raw, (bytes, bytearray)):
                    continue  # started 前的二进制帧不正常，丢弃
                self._route_event(raw, state)
                if state.error is not None:
                    raise state.error
                if state.started:
                    return
        except TimeoutError as e:
            await self._drop_ws()
            raise TTSError("timeout waiting for task-started") from e

    def _route_event(self, raw: str, state: _Task) -> None:
        """一条服务端文本消息 → 更新 state；task-failed 记 error 待上抛。"""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        header = msg.get("header", {})
        event = header.get("event")
        if event == "task-started":
            state.started = True
        elif event == "task-finished":
            state.done.set()
        elif event == "task-failed":
            code = header.get("error_code", "?")
            text = header.get("error_message", "")
            state.error = TTSError(f"task-failed {code}: {text}")
            state.done.set()
        # result-generated（usage 等元数据）与未知事件忽略
