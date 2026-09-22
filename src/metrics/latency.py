"""Latency metrics：doc 06 §3 九时间戳逐轮记录 + SEFA + barge-in stop latency。

口径（low-latency-pipeline C1/C5：分阶段预算、逐轮记录、按阶段归因）：

    t_user_speech_end    用户最近一次 VAD 停顿（USER_SPEECH_STOPPED）
    t_turn_confirmed     USER_TURN_COMPLETE（Smart Turn 裁决，含 source）
    t_asr_final          轮内最后一次 ASR_FINAL
    t_llm_first_token    LLM 流式输出首 token（投机命中时该等待已被摊掉）
    t_tts_request        pipeline 发起 tts.stream_audio
    t_first_audio        TTS 产出首字节音频
    t_playback_start     首字节写入播放器（≈开始出声，误差≈输出 buffer）
    t_interrupt_detected barge-in：打断 utterance 的 USER_SPEECH_STARTED 时刻
                         （从 bus history 回溯，含用户侧检测延迟）
    t_playback_stopped   PLAYBACK_STOPPED（自然播完 reason=completed /
                         打断 reason=interrupted / 空回复 reason=noop）

派生指标：
- sefa_ms = t_first_audio - t_user_speech_end（目标区间 500–800ms，非硬承诺）
- barge_in_stop_ms = t_playback_stopped - t_interrupt_detected
  （仅 reason=interrupted 时给出）

投机执行观测（turn-taking C6）：
- prompt_prebuilt_at：最近一次 partial 驱动的 prompt 预构造时刻
- speculative：hit（turn_complete 文本与预构造一致，直接发 LLM）/
  miss（不一致重建）/none（无 partial 可预构造）

落盘：data/latency_log/session_<UTC>.jsonl 每轮一行（closed 时追加），
close() 时写同目录 summary_<UTC>.json（各阶段 mean/p50/p95）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from conversation.events import Event, EventBus, EventType

#: doc 06 §3 的九个规范时间戳字段名（latency log 逐轮必有键，值为 None 表示未发生）。
NINE_TIMESTAMPS: tuple[str, ...] = (
    "t_user_speech_end",
    "t_turn_confirmed",
    "t_asr_final",
    "t_llm_first_token",
    "t_tts_request",
    "t_first_audio",
    "t_playback_start",
    "t_interrupt_detected",
    "t_playback_stopped",
)


def _percentile(values: list[float], q: float) -> float | None:
    """线性插值分位数；空样本返回 None。"""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": round(sum(values) / len(values), 1),
        "p50": round(_percentile(values, 0.50), 1),
        "p95": round(_percentile(values, 0.95), 1),
    }


@dataclass
class TurnRecord:
    """一次"用户轮次 → agent 回应"交换的延迟账本。

    时间戳一律为 now_fn()（默认 time.monotonic）秒值；序列化时换算成
    相对 session 起点的毫秒。None = 该环节本轮未发生。
    """

    turn_id: int
    opened_at: float
    # --- doc 06 §3 九时间戳 ---
    t_user_speech_end: float | None = None
    t_turn_confirmed: float | None = None
    t_asr_final: float | None = None
    t_llm_first_token: float | None = None
    t_tts_request: float | None = None
    t_first_audio: float | None = None
    t_playback_start: float | None = None
    t_interrupt_detected: float | None = None
    t_playback_stopped: float | None = None
    # --- 扩展观测 ---
    prompt_prebuilt_at: float | None = None
    prompt_prebuilt_text: str | None = None
    t_llm_request: float | None = None
    speculative: str | None = None  # hit / miss / none
    turn_source: str | None = None  # model / silence_fallback
    user_text: str = ""
    reply_speech: str | None = None
    reply_emotion: str | None = None
    stop_reason: str | None = None  # completed / interrupted / noop / error
    closed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def sefa_ms(self) -> float | None:
        if self.t_first_audio is None or self.t_user_speech_end is None:
            return None
        return (self.t_first_audio - self.t_user_speech_end) * 1000

    @property
    def barge_in_stop_ms(self) -> float | None:
        if (
            self.stop_reason != "interrupted"
            or self.t_playback_stopped is None
            or self.t_interrupt_detected is None
        ):
            return None
        return (self.t_playback_stopped - self.t_interrupt_detected) * 1000

    def offset_ms(self, key: str, base: float | None) -> float | None:
        """某时间戳相对 base（默认 t_user_speech_end）的毫秒偏移。"""
        ts = getattr(self, key, None)
        if ts is None or base is None:
            return None
        return (ts - base) * 1000

    def to_dict(self, session_start: float) -> dict[str, Any]:
        row: dict[str, Any] = {"turn_id": self.turn_id}
        for key in NINE_TIMESTAMPS:
            ts = getattr(self, key)
            row[key] = (
                round((ts - session_start) * 1000, 1) if ts is not None else None
            )
        row["prompt_prebuilt_at"] = (
            round((self.prompt_prebuilt_at - session_start) * 1000, 1)
            if self.prompt_prebuilt_at is not None
            else None
        )
        row["t_llm_request"] = (
            round((self.t_llm_request - session_start) * 1000, 1)
            if self.t_llm_request is not None
            else None
        )
        row["sefa_ms"] = (
            round(self.sefa_ms, 1) if self.sefa_ms is not None else None
        )
        row["barge_in_stop_ms"] = (
            round(self.barge_in_stop_ms, 1)
            if self.barge_in_stop_ms is not None
            else None
        )
        row["speculative"] = self.speculative
        row["turn_source"] = self.turn_source
        row["stop_reason"] = self.stop_reason
        row["user_text"] = self.user_text
        row["reply_speech"] = self.reply_speech
        row["reply_emotion"] = self.reply_emotion
        if self.extra:
            row["extra"] = self.extra
        return row


class LatencyLog:
    """订阅 bus 的逐轮延迟记录器；无 bus 时也可纯手动打点（测试友好）。

    record 生命周期：USER_TURN_STARTED 开账 → PLAYBACK_STOPPED 或下一个
    USER_TURN_STARTED 封账写盘。一个 record 对应一次"用户轮次→agent 回应"
    交换；barge-in 的 interrupt/stopped 记在被切掉的那一轮上。
    """

    def __init__(
        self,
        bus: EventBus | None = None,
        *,
        outdir: str | Path | None = None,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._now = now_fn
        self._bus = bus
        self._t0 = now_fn()
        self._wall_start = datetime.now(timezone.utc)
        self.records: list[TurnRecord] = []
        self.current: TurnRecord | None = None
        self._path: Path | None = None
        self._outdir = Path(outdir) if outdir is not None else None
        if self._outdir is not None:
            self._outdir.mkdir(parents=True, exist_ok=True)
            stamp = self._wall_start.strftime("%Y%m%dT%H%M%SZ")
            self._path = self._outdir / f"session_{stamp}.jsonl"
        if bus is not None:
            self._subscribe(bus)

    @property
    def path(self) -> Path | None:
        return self._path

    # ---- 打点 API（事件面覆盖不到的时刻由 pipeline 显式打） ----

    def note(self, key: str, ts: float | None = None) -> None:
        """往当前轮次写任意时间戳字段（如 prompt_prebuilt_at）。"""
        if self.current is not None and hasattr(self.current, key):
            setattr(self.current, key, ts if ts is not None else self._now())

    # ---- 事件订阅 ----

    def _subscribe(self, bus: EventBus) -> None:
        for et in (
            EventType.USER_TURN_STARTED,
            EventType.USER_SPEECH_STOPPED,
            EventType.USER_TURN_COMPLETE,
            EventType.ASR_FINAL,
            EventType.PROMPT_PREBUILT,
            EventType.LLM_STARTED,
            EventType.LLM_TOKEN,
            EventType.TTS_STARTED,
            EventType.FIRST_AUDIO,
            EventType.AGENT_SPEAKING,
            EventType.AGENT_INTERRUPTED,
            EventType.PLAYBACK_STOPPED,
            EventType.AGENT_REPLY,
        ):
            bus.subscribe(et, self._on_event)

    def _on_event(self, event: Event) -> None:
        match event.type:
            case EventType.USER_TURN_STARTED:
                # 新用户轮次开账。barge-in 时本事件先于六步打断派发：有在途
                # 响应（LLM 在途或已出声）的旧轮次不能封账，留给随后的
                # PLAYBACK_STOPPED(interrupted) 记 t_interrupt/stopped；
                # 无在途响应的旧轮次与更早遗留的 stale 轮次按 superseded 清扫。
                stale = [r for r in self.records if not r.closed]
                for rec in stale[:-1]:
                    self._close(rec, reason="superseded")
                if stale:
                    last = stale[-1]
                    if (
                        last.t_llm_request is None
                        and last.t_playback_start is None
                    ):
                        self._close(last, reason="superseded")
                rec = TurnRecord(
                    turn_id=int(event.payload.get("turn_id") or 0),
                    opened_at=event.ts,
                )
                if event.payload.get("barge_in"):
                    rec.extra["barge_in"] = True
                self.records.append(rec)
                self.current = rec
            case EventType.USER_SPEECH_STOPPED:
                if self.current is not None:
                    self.current.t_user_speech_end = event.ts
            case EventType.USER_TURN_COMPLETE:
                if self.current is not None:
                    self.current.t_turn_confirmed = event.ts
                    self.current.turn_source = event.payload.get("source")
                    self.current.user_text = event.payload.get("text", "")
            case EventType.ASR_FINAL:
                if self.current is not None:
                    # 轮内多次断句 final：取最后一次（最贴近轮次完成时刻）。
                    self.current.t_asr_final = event.ts
                elif self.records and self.records[-1].t_asr_final is None:
                    # 轮次已封账的迟到尾帧：仅当该轮从未记过 final 才回填。
                    self.records[-1].t_asr_final = event.ts
            case EventType.PROMPT_PREBUILT:
                if self.current is not None:
                    self.current.prompt_prebuilt_at = event.ts
                    self.current.prompt_prebuilt_text = event.payload.get("text")
            case EventType.LLM_STARTED:
                if self.current is not None:
                    self.current.t_llm_request = event.ts
                    self.current.speculative = event.payload.get("speculative")
            case EventType.LLM_TOKEN:
                if (
                    self.current is not None
                    and self.current.t_llm_first_token is None
                ):
                    self.current.t_llm_first_token = event.ts
            case EventType.TTS_STARTED:
                if self.current is not None:
                    self.current.t_tts_request = event.ts
            case EventType.FIRST_AUDIO:
                if self.current is not None:
                    self.current.t_first_audio = event.ts
            case EventType.AGENT_SPEAKING:
                if (
                    self.current is not None
                    and self.current.t_playback_start is None
                ):
                    self.current.t_playback_start = event.ts
            case EventType.AGENT_INTERRUPTED:
                rec = self._record_for_interrupt()
                if rec is not None:
                    # 用户重新开口 → 停播：起点回溯到触发它的 USER_SPEECH_STARTED
                    rec.t_interrupt_detected = self._last_speech_started_ts(
                        before=event.ts
                    ) or event.ts
            case EventType.AGENT_REPLY:
                if self.current is not None:
                    self.current.reply_speech = event.payload.get("speech")
                    self.current.reply_emotion = event.payload.get("emotion")
            case EventType.PLAYBACK_STOPPED:
                # 打断路径先封被切轮次；自然播完封当前轮。
                rec = self._record_for_playback_stop(event)
                if rec is not None:
                    rec.t_playback_stopped = event.ts
                    self._close(
                        rec, reason=str(event.payload.get("reason") or "stopped")
                    )

    # ---- 封账与写盘 ----

    def _record_for_interrupt(self) -> TurnRecord | None:
        """AGENT_INTERRUPTED 归属：被打断的是"有在途响应"的那一轮。

        SPEAKING 中被打断 → t_playback_start 已记；THINKING 中被打断
        （LLM 在途未出声）→ t_llm_request 已记。两者都算"被切的轮次"。
        """
        for rec in reversed(self.records):
            if not rec.closed and (
                rec.t_playback_start is not None
                or rec.t_llm_request is not None
            ):
                return rec
        return self.current

    def _record_for_playback_stop(self, event: Event) -> TurnRecord | None:
        reason = event.payload.get("reason")
        if reason == "interrupted":
            for rec in reversed(self.records):
                if not rec.closed and rec.t_interrupt_detected is not None:
                    return rec
        return self.current

    def _last_speech_started_ts(self, *, before: float) -> float | None:
        if self._bus is None:
            return None
        for ev in reversed(self._bus.history):
            if ev.ts > before:
                continue
            if ev.type == EventType.USER_SPEECH_STARTED:
                return ev.ts
        return None

    def _close(self, rec: TurnRecord, *, reason: str) -> None:
        rec.closed = True
        rec.stop_reason = rec.stop_reason or reason
        if rec is self.current:
            self.current = None
        self._write(rec)

    def _write(self, rec: TurnRecord) -> None:
        if self._path is None:
            return
        row = rec.to_dict(self._t0)
        row["wall_ts"] = self._wall_start.isoformat(timespec="seconds")
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ---- 汇总 ----

    def summary(self) -> dict[str, Any]:
        """分阶段统计（C5：回归按阶段定位，不只看端到端均值）。"""
        done = [r for r in self.records if r.closed]
        sefa = [r.sefa_ms for r in done if r.sefa_ms is not None]
        barge = [
            r.barge_in_stop_ms for r in done if r.barge_in_stop_ms is not None
        ]
        stage = lambda a, b: [
            (getattr(r, b) - getattr(r, a)) * 1000
            for r in done
            if getattr(r, a) is not None and getattr(r, b) is not None
        ]
        spec = {"hit": 0, "miss": 0, "none": 0}
        for r in done:
            if r.speculative in spec:
                spec[r.speculative] += 1
        sources: dict[str, int] = {}
        for r in done:
            if r.turn_source:
                sources[r.turn_source] = sources.get(r.turn_source, 0) + 1
        return {
            "turns": len(done),
            "sefa_ms": _summary(sefa),
            "barge_in_stop_ms": _summary(barge),
            "stages_ms": {
                "speech_end_to_turn_confirmed": _summary(
                    stage("t_user_speech_end", "t_turn_confirmed")
                ),
                "turn_confirmed_to_asr_final": _summary(
                    stage("t_turn_confirmed", "t_asr_final")
                ),
                "llm_request_to_first_token": _summary(
                    stage("t_llm_request", "t_llm_first_token")
                ),
                "tts_request_to_first_audio": _summary(
                    stage("t_tts_request", "t_first_audio")
                ),
                "first_audio_to_playback_start": _summary(
                    stage("t_first_audio", "t_playback_start")
                ),
            },
            "speculative": spec,
            "turn_source": sources,
        }

    def close(self) -> Path | None:
        """写 session summary；返回 summary 文件路径（无 outdir 则 None）。"""
        if self.current is not None and not self.current.closed:
            self._close(self.current, reason="session_end")
        if self._outdir is None:
            return None
        stamp = self._wall_start.strftime("%Y%m%dT%H%M%SZ")
        summary_path = self._outdir / f"summary_{stamp}.json"
        summary_path.write_text(
            json.dumps(
                {
                    "session_start": self._wall_start.isoformat(
                        timespec="seconds"
                    ),
                    "log": str(self._path),
                    **self.summary(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return summary_path
