"""Terminal UI：doc 06 §2 的会话界面（状态标记 + 对话流 + 逐轮延迟数字）。

渲染约定（对照 doc §2 示例）：

    [LISTENING]
    YOU: <partial / final 转写>
    Turn: INCOMPLETE | COMPLETE
    ASR final: +210ms
    LLM first token: +355ms
    TTS first audio: +510ms
    PAIMON:
    <speech>
    [SPEAKING]
    [INTERRUPTED +93ms]

延迟行以"用户说完"（t_user_speech_end）为 +0ms 基准。STATE_CHANGED 正常
迁移逐行打 `[STATE]`；INTERRUPTED 延迟到 PLAYBACK_STOPPED 到达时带
barge-in stop latency 一次性打印（doc 的 `[INTERRUPTED +93ms]` 形态）。
无任何第三方依赖，输出走注入的 text stream（默认 sys.stdout，测试用
StringIO）；Windows 控制台中文乱码见 F-032——入口负责 reconfigure。
"""

from __future__ import annotations

import sys
import time
from typing import Callable, TextIO

from conversation.events import Event, EventBus, EventType
from conversation.state_machine import ConversationState
from metrics.latency import LatencyLog


def _fmt_ms(value: float | None) -> str:
    return f"+{value:.0f}ms" if value is not None else "n/a"


class TerminalUI:
    """事件驱动的终端渲染器；metrics 提供逐轮时间戳口径。"""

    def __init__(
        self,
        bus: EventBus,
        metrics: LatencyLog,
        *,
        out: TextIO | None = None,
        now_fn: Callable[[], float] = time.monotonic,
        echo_partials: bool = True,
    ) -> None:
        self._out = out if out is not None else sys.stdout
        self._metrics = metrics
        self._now = now_fn
        self._echo_partials = echo_partials
        self._last_partial = ""
        self._interrupt_pending = False  # 已迁 INTERRUPTED，等 +ms 数字
        for et in (
            EventType.STATE_CHANGED,
            EventType.ASR_PARTIAL,
            EventType.ASR_FINAL,
            EventType.TURN_INCOMPLETE,
            EventType.USER_TURN_COMPLETE,
            EventType.PROMPT_PREBUILT,
            EventType.LLM_STARTED,
            EventType.FIRST_AUDIO,
            EventType.AGENT_REPLY,
            EventType.PLAYBACK_STOPPED,
            EventType.PIPELINE_ERROR,
        ):
            bus.subscribe(et, self._on_event)

    # ---- 渲染 ----

    def _print(self, text: str = "") -> None:
        print(text, file=self._out, flush=True)

    def _on_event(self, event: Event) -> None:
        match event.type:
            case EventType.STATE_CHANGED:
                self._on_state_changed(event)
            case EventType.ASR_PARTIAL:
                text = event.payload.get("text", "")
                if self._echo_partials and text and text != self._last_partial:
                    self._last_partial = text
                    self._print(f"YOU: {text}")
            case EventType.ASR_FINAL:
                text = event.payload.get("text", "")
                if text and text != self._last_partial:
                    self._print(f"YOU: {text}")
                self._last_partial = text
            case EventType.TURN_INCOMPLETE:
                self._print("Turn: INCOMPLETE")
            case EventType.USER_TURN_COMPLETE:
                self._print("Turn: COMPLETE")
            case EventType.PROMPT_PREBUILT:
                rec = self._metrics.current
                base = rec.t_user_speech_end if rec else None
                off = (event.ts - base) * 1000 if base is not None else None
                self._print(f"  (prompt prebuilt {_fmt_ms(off)})")
            case EventType.LLM_STARTED:
                spec = event.payload.get("speculative")
                if spec:
                    self._print(f"  (llm request, speculation={spec})")
            case EventType.FIRST_AUDIO:
                self._print_latency_block()
            case EventType.AGENT_REPLY:
                speech = event.payload.get("speech") or ""
                emotion = event.payload.get("emotion")
                tag = f" ({emotion})" if emotion and emotion != "neutral" else ""
                if speech.strip():
                    self._print(f"PAIMON{tag}:")
                    self._print(speech)
                else:
                    self._print("PAIMON: (没说——NOOP)")
            case EventType.PIPELINE_ERROR:
                self._print(
                    f"[error:{event.payload.get('stage')}] "
                    f"{event.payload.get('error')}"
                )
            case EventType.PLAYBACK_STOPPED:
                if event.payload.get("reason") == "interrupted":
                    rec = self._interrupted_record()
                    ms = rec.barge_in_stop_ms if rec else None
                    self._print(f"[INTERRUPTED {_fmt_ms(ms)}]")
                    self._interrupt_pending = False

    def _on_state_changed(self, event: Event) -> None:
        to = event.payload.get("to")
        if to == ConversationState.INTERRUPTED:
            # doc 形态是 `[INTERRUPTED +93ms]`：等停播数字到了一起打
            self._interrupt_pending = True
            return
        if self._interrupt_pending:
            # 停播事件没跟上（异常路径），先把未带数字的标记补上
            self._print("[INTERRUPTED]")
            self._interrupt_pending = False
        self._print(f"[{to}]")

    def _print_latency_block(self) -> None:
        """doc §2 的三行延迟：以 t_user_speech_end 为 +0 基准。"""
        rec = self._metrics.current
        base = rec.t_user_speech_end if rec else None
        self._print(f"ASR final: {_fmt_ms(rec.offset_ms('t_asr_final', base) if rec else None)}")
        self._print(f"LLM first token: {_fmt_ms(rec.offset_ms('t_llm_first_token', base) if rec else None)}")
        self._print(f"TTS first audio: {_fmt_ms(rec.offset_ms('t_first_audio', base) if rec else None)}")

    def _interrupted_record(self):
        for rec in reversed(self._metrics.records):
            if rec.t_interrupt_detected is not None:
                return rec
        return None
