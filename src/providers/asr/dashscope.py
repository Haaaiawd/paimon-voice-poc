"""DashScope 流式 ASR adapter：paraformer-realtime-v2 WebSocket duplex。

协议（与 dashscope SDK Recognition 相同 wire format，自研 async 实现）：
  connect(Authorization: Bearer) → run-task → task-started
    → 持续发二进制 PCM → 持续收 result-generated
    → finish-task → task-finished → close。

参数按 streaming-asr-zh 决策树：
  C1 semantic_punctuation_enabled=False —— VAD 断句，低延迟交互档；
  C2 max_sentence_silence=500ms 初始值 —— 服务端断句只决定 ASR_FINAL
     何时落地，默认 1300ms 会把语言层信号拖慢；
  C4 disfluency_removal_enabled=False —— "嗯/那个"是犹豫信号，
     TurnManager 要看语言层完整性；
  C3 事件面只含 partial/final —— 服务端 sentence_end 只映射 kind="final"，
     绝不直驱状态机，轮次裁决归 Smart Turn。

网络：websockets.connect(proxy=None) 显式直连——本机 Clash 代理给每连接
+~1.9s TLS 开销（.env.example / F-030），且不改进程级 env，不影响其它组件。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from .base import ASREvent, ASRProvider

DEFAULT_MODEL = "paraformer-realtime-v2"
DEFAULT_WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

# streaming-asr-zh C1/C2/C4 的项目默认参数。
DEFAULT_PARAMETERS: Mapping[str, Any] = {
    "semantic_punctuation_enabled": False,  # C1：VAD 断句，交互档
    "max_sentence_silence": 500,  # C2：服务端断句静音阈值，进 metrics 后再调
    "disfluency_removal_enabled": False,  # C4：保留语气词（犹豫信号）
    "language_hints": ["zh"],
    "heartbeat": True,  # 静音期保活
}


class ASRError(Exception):
    """ASR provider 侧错误：握手失败 / task-failed / 连接异常中断。"""


def _run_task_message(task_id: str, payload: Mapping[str, Any]) -> str:
    return json.dumps(
        {
            "header": {
                "action": "run-task",
                "task_id": task_id,
                "streaming": "duplex",
            },
            "payload": payload,
        },
        ensure_ascii=False,
    )


def _finish_task_message(task_id: str) -> str:
    return json.dumps(
        {
            "header": {
                "action": "finish-task",
                "task_id": task_id,
                "streaming": "duplex",
            },
            "payload": {"input": {}},
        },
        ensure_ascii=False,
    )


def _sentence_to_event(sentence: Mapping[str, Any]) -> ASREvent | None:
    """服务端 sentence → ASREvent。

    final 判定：sentence_end=true 或 end_time 非空（v1/v2 两种格式都覆盖）。
    heartbeat 句与空文本不产生事件。raw 保留整句供 metrics/调试。
    """
    if sentence.get("heartbeat"):
        return None
    text = sentence.get("text") or ""
    if not text:
        return None
    is_final = bool(sentence.get("sentence_end")) or (
        sentence.get("end_time") is not None
    )
    return ASREvent(
        kind="final" if is_final else "partial",
        text=text,
        raw=dict(sentence),
    )


class DashScopeASR(ASRProvider):
    """paraformer-realtime-v2 流式识别。

    `stream()` 消费 PCM（默认 16kHz mono int16）异步流，产出 partial/final
    ASREvent；audio 结束时发 finish-task 并收尾。构造参数即决策树默认，
    实测后可按 metrics 调整（max_sentence_silence 等）。
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        url: str = DEFAULT_WS_URL,
        audio_format: str = "pcm",
        parameters: Mapping[str, Any] | None = None,
        open_timeout: float = 15.0,
        proxy: str | None = None,
        connector: Any | None = None,
    ) -> None:
        if not api_key:
            raise ASRError("DashScopeASR: api_key is required")
        self._api_key = api_key
        self.model = model
        self.url = url
        self.audio_format = audio_format
        self.parameters = {**DEFAULT_PARAMETERS, **(parameters or {})}
        self.open_timeout = open_timeout
        # proxy=None → websockets 直连，绕开 env 里的 HTTP(S)_PROXY（Clash 坑）
        self.proxy = proxy
        # 测试可注入替代 connector（签名同 websockets.connect）
        self._connect = connector or websockets.connect
        self._ws: ClientConnection | None = None
        self._closing = False
        # 延迟观测：首帧音频发出 → 首个结果事件的毫秒数（demo/metrics 用）
        self.first_event_latency_ms: float | None = None

    async def stream(
        self,
        audio: AsyncIterable[bytes],
        *,
        sample_rate: int = 16000,
        **kwargs: Any,
    ) -> AsyncIterator[ASREvent]:
        task_id = uuid.uuid4().hex
        parameters = {
            **self.parameters,
            "sample_rate": sample_rate,
            "format": self.audio_format,
            **kwargs,
        }
        run_task = _run_task_message(
            task_id,
            {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": self.model,
                "parameters": parameters,
                "input": {},
            },
        )

        self._closing = False
        self.first_event_latency_ms = None
        finished = False
        first_audio_ts: float | None = None
        async with self._connect(
            self.url,
            additional_headers={"Authorization": f"Bearer {self._api_key}"},
            open_timeout=self.open_timeout,
            proxy=self.proxy,
        ) as ws:
            self._ws = ws
            try:
                await ws.send(run_task)
                await self._wait_task_started(ws, task_id)

                pump_error: list[Exception] = []

                async def pump() -> None:
                    nonlocal first_audio_ts
                    try:
                        async for frame in audio:
                            if first_audio_ts is None:
                                first_audio_ts = time.monotonic()
                            await ws.send(frame)
                        await ws.send(_finish_task_message(task_id))
                    except websockets.ConnectionClosed:
                        pass  # 接收侧会以协议事件/错误收尾
                    except Exception as e:
                        pump_error.append(e)
                        await ws.close()  # 音频源挂了 → 让接收侧退出

                sender = asyncio.create_task(pump())
                try:
                    async for raw in ws:
                        if not isinstance(raw, str):
                            continue  # ASR 无二进制下行
                        for event, is_finished in self._events_from_message(raw):
                            if is_finished:
                                finished = True
                            if event is not None:
                                if (
                                    self.first_event_latency_ms is None
                                    and first_audio_ts is not None
                                ):
                                    self.first_event_latency_ms = (
                                        time.monotonic() - first_audio_ts
                                    ) * 1000
                                yield event
                        if finished:
                            break
                finally:
                    if not sender.done():
                        sender.cancel()
                        await asyncio.gather(sender, return_exceptions=True)
                if pump_error:
                    raise ASRError(
                        f"audio source failed: {pump_error[0]}"
                    ) from pump_error[0]
                if not finished and not self._closing:
                    raise ASRError("connection closed before task-finished")
            finally:
                self._ws = None

    async def close(self) -> None:
        """关闭进行中的 stream；幂等。"""
        self._closing = True
        ws, self._ws = self._ws, None
        if ws is not None:
            await ws.close()

    async def _wait_task_started(self, ws: ClientConnection, task_id: str) -> None:
        """run-task 后首条消息必须是 task-started；task-failed 抛 ASRError。"""

        async def _wait() -> None:
            async for raw in ws:
                if not isinstance(raw, str):
                    raise ASRError(
                        f"unexpected binary message before task-started: {len(raw)}B"
                    )
                msg = json.loads(raw)
                event = msg.get("header", {}).get("event")
                if event == "task-started":
                    return
                if event == "task-failed":
                    raise ASRError(self._failure_text(msg))
                raise ASRError(f"unexpected event before task-started: {event!r}")
            raise ASRError("connection closed before task-started")

        try:
            await asyncio.wait_for(_wait(), timeout=self.open_timeout)
        except TimeoutError as e:
            raise ASRError("timeout waiting for task-started") from e

    def _events_from_message(
        self, raw: str
    ) -> list[tuple[ASREvent | None, bool]]:
        """解析一条服务端文本消息 → [(event|None, finished)]。

        result-generated / task-finished 的 payload.output.sentence 都可能
        携带一句结果；task-failed 抛 ASRError；其余事件忽略。
        """
        msg = json.loads(raw)
        event = msg.get("header", {}).get("event")
        if event == "result-generated":
            sentence = (msg.get("payload") or {}).get("output", {}).get(
                "sentence"
            ) or {}
            return [(_sentence_to_event(sentence), False)]
        if event == "task-finished":
            sentence = (msg.get("payload") or {}).get("output", {}).get(
                "sentence"
            ) or {}
            return [(_sentence_to_event(sentence), True)]
        if event == "task-failed":
            raise ASRError(self._failure_text(msg))
        return [(None, False)]  # 未知事件忽略

    @staticmethod
    def _failure_text(msg: Mapping[str, Any]) -> str:
        header = msg.get("header") or {}
        code = header.get("error_code", "?")
        text = header.get("error_message", "")
        return f"task-failed {code}: {text}"
