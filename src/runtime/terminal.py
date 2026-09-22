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
        self._interrupt_ts: float | None = None
        self._latency_printed = False  # 本轮延迟三行已打出（补 late-final 用）
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
            EventType.AGENT_INTERRUPTED,
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
                if self._latency_printed:
                    # 延迟块已按 FIRST_AUDIO 时刻打出；迟到 final（如未开
                    # transcript 闸门的脚本链路）补一行，不留 n/a。
                    rec = self._metrics.current
                    base = (
                        (rec.t_user_speech_end or rec.t_turn_confirmed)
                        if rec
                        else None
                    )
                    off = (
                        rec.offset_ms("t_asr_final", base) if rec else None
                    )
                    if off is not None:
                        self._print(f"ASR final: {_fmt_ms(off)}")
            case EventType.TURN_INCOMPLETE:
                self._print("Turn: INCOMPLETE")
            case EventType.USER_TURN_COMPLETE:
                self._latency_printed = False
                self._print("Turn: COMPLETE")
            case EventType.PROMPT_PREBUILT:
                rec = self._metrics.current
                base = rec.t_user_speech_end if rec else None
                if base is None:
                    self._print("  (prompt prebuilt)")
                else:
                    off = (event.ts - base) * 1000
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
            case EventType.AGENT_INTERRUPTED:
                self._interrupt_ts = event.ts
            case EventType.PLAYBACK_STOPPED:
                # 兜底：interrupted 停播但 INTERRUPTED→LISTENING 迁移没发生过
                # （如 THINKING 中被打断直接回 LISTENING）也要出数字行。
                if (
                    event.payload.get("reason") == "interrupted"
                    and self._interrupt_pending
                ):
                    self._print_interrupted_line()
                    self._print("[LISTENING]")
                    self._interrupt_pending = False

    def _on_state_changed(self, event: Event) -> None:
        to = event.payload.get("to")
        if to == ConversationState.INTERRUPTED:
            # doc 形态是 `[INTERRUPTED +93ms]`：PLAYBACK_STOPPED 驱动的
            # INTERRUPTED→LISTENING 迁移到达时带数字一次性打印。
            self._interrupt_pending = True
            return
        if self._interrupt_pending:
            if to == ConversationState.LISTENING:
                # 停播已发生（嵌套派发：本迁移即 PLAYBACK_STOPPED 引起），
                # 按 doc 形态先 INTERRUPTED 行再 LISTENING。
                self._print_interrupted_line()
            else:
                # 非典型路径：没等到停播，先补无数字标记
                self._print("[INTERRUPTED]")
            self._interrupt_pending = False
        self._print(f"[{to}]")

    def _print_interrupted_line(self) -> None:
        """`[INTERRUPTED +Nms]`：打断检测到停播的耗时（barge-in stop latency）。"""
        ms = None
        if self._interrupt_ts is not None:
            ms = (self._now() - self._interrupt_ts) * 1000
        self._print(f"[INTERRUPTED {_fmt_ms(ms)}]")

    def _print_latency_block(self) -> None:
        """doc §2 的三行延迟：以 t_user_speech_end 为 +0 基准。

        无 VAD 停顿的轮次（silence_fallback/asr_implied）以
        t_turn_confirmed 做基准，保证三行始终有数字可读。
        """
        rec = self._metrics.current
        base = None
        if rec is not None:
            base = rec.t_user_speech_end or rec.t_turn_confirmed
        self._latency_printed = True
        self._print(f"ASR final: {_fmt_ms(rec.offset_ms('t_asr_final', base) if rec else None)}")
        self._print(f"LLM first token: {_fmt_ms(rec.offset_ms('t_llm_first_token', base) if rec else None)}")
        self._print(f"TTS first audio: {_fmt_ms(rec.offset_ms('t_first_audio', base) if rec else None)}")
