"""TASK-012 MVP 验收驱动：doc 06 §4 用例 A–G + ≥10 分钟连续会话 soak。

无网络/无声卡环境下用脚本化 provider（src/runtime/simulated.py）驱动真实
VoicePipeline + ConversationCore，按真实时序定速送帧（32ms/frame）：

- `--cases`：A–G 逐用例独立场景，断言轮次/打断/静默/主动性行为；
- `--soak`：单会话 ≥10 分钟连续对话（A–G 行为 + 填充轮次），检验长跑
  稳定性与 latency log 完整性；
- `--all`（默认）：先跑用例再跑 soak。

产出：
- `data/mvp_acceptance/results_<UTC>.json` — 用例 pass/fail + 断言明细；
- `data/mvp_acceptance/latency/session_*.jsonl` — 用例逐轮账本；
- `data/latency_log/session_*.jsonl` + `summary_*.json` — soak 账本（主目录）；
- `data/mvp_acceptance/playback_soak.wav` — soak "播出"音频落盘。

注：脚本化 provider 的 LLM/TTS 延迟是模拟值（first_token 0.35s），SEFA 数字
只证明链路记账正确；真实供应商延迟以 data/llm_benchmark（TASK-003）为准。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from character.agent import CharacterAgent  # noqa: E402
from conversation.core import ConversationCore  # noqa: E402
from conversation.events import EventType  # noqa: E402
from conversation.state_machine import ConversationState  # noqa: E402
from metrics.latency import LatencyLog  # noqa: E402
from runtime.pipeline import VoicePipeline  # noqa: E402
from runtime.simulated import (  # noqa: E402
    ScriptedASR,
    ScriptedLLM,
    ScriptedTurn,
    ScriptedVAD,
    ToneTTS,
    WavSinkPlayer,
    silence_frame,
    tone_frame,
)

FRAME_S = 0.032  # 16kHz int16 mono 32ms = 1024B（与 MicCapture/Silero 约定一致）
#: ScriptedASR 需要连续 6 静音帧（≈0.19s）才断句；用例间留 ≥0.35s 保证分离。
MIN_REGION_GAP_S = 0.35

RESULTS_DIR = ROOT / "data" / "mvp_acceptance"
SOAK_WAV = RESULTS_DIR / "playback_soak.wav"


# ---------------------------------------------------------------- 脚本化增强


class PaceTTS(ToneTTS):
    """按文本长度决定合成时长的假 TTS：长回复真占播放时长，短回复快播完。

    用于让 barge-in 落在大段回复中段、互怼短句在间隙内播完。
    """

    def __init__(
        self,
        *,
        sample_rate: int = 24000,
        sec_per_char: float = 0.10,
        min_secs: float = 0.5,
        max_secs: float = 9.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(sample_rate=sample_rate, **kwargs)
        self._sec_per_char = sec_per_char
        self._min_secs = min_secs
        self._max_secs = max_secs

    async def stream_audio(self, chunks, **kwargs):
        self._cancelled = False
        async for text in chunks:
            if self._cancelled:
                return
            self.synthesized.append(text)
            secs = min(
                max(len(text) * self._sec_per_char, self._min_secs),
                self._max_secs,
            )
            total = int(self.sample_rate * secs)
            per = max(total // self._chunk_frames, 1)
            for _ in range(self._chunk_frames):
                if self._cancelled:
                    return
                yield _tone_pcm(per, self.sample_rate)


def _tone_pcm(n_samples: int, sample_rate: int, *, freq: float = 440.0):
    import math
    import struct

    samples = [
        int(6000 * math.sin(2 * math.pi * freq * i / sample_rate))
        for i in range(n_samples)
    ]
    return struct.pack(f"<{n_samples}h", *samples)


#: 按 user 文本关键词选回复；未命中走轮换池（保持派蒙式短口语）。
_LONG_REPLY = (
    "派蒙是提瓦特最棒的向导哦！跟着派蒙走，宝箱、美食、还有"
    "各种各样好玩的冒险，一个都不会错过的！"
)
_KEYWORD_REPLIES = [
    ("吃什么", "当然是甜甜花酿鸡！派蒙强烈推荐！"),
    ("讲讲你自己", _LONG_REPLY),
    ("想清楚", "哼，派蒙想得可清楚了，不用你教！"),
    ("还在吗", "在的在的！派蒙一直都在！"),
    ("派蒙", "叫我干嘛？派蒙随时待命！"),
    ("烦不烦", "你才烦！派蒙这是在关心你！"),
    ("你才烦", "哼，说不过就耍赖是吧？"),
    ("你再说", "说就说！派蒙怕你哦？"),
    ("说就说", "你——气死派蒙了！"),
]
_FALLBACK_POOL = [
    "哦？继续说，派蒙听着呢！",
    "嗯嗯，派蒙懂你的意思！",
    "嘿嘿，这个派蒙知道！",
    "真的吗？那也太厉害了吧！",
    "派蒙觉得你说得对！",
]
_pool_idx = 0


def reply_for(messages: Any) -> dict[str, Any]:
    """ScriptedLLM 的 callable reply：按本轮 user 文本/意图选回复。"""
    global _pool_idx
    content = str(messages[-1].get("content", "")) if messages else ""
    m = re.search(r'user: "(.*?)"', content, re.DOTALL)
    user_text = m.group(1) if m else ""
    if "initiative_reason:" in content:
        speech = "喂——你怎么突然不说话啦？派蒙都要无聊死了！"
        return _reply(speech, emotion="pout", energy=0.5)
    for kw, speech in _KEYWORD_REPLIES:
        if kw in user_text:
            return _reply(speech)
    speech = _FALLBACK_POOL[_pool_idx % len(_FALLBACK_POOL)]
    _pool_idx += 1
    return _reply(speech)


def _reply(speech: str, *, emotion: str = "smug", energy: float = 0.6):
    return {
        "speech": speech,
        "emotion": emotion,
        "energy": energy,
        "should_continue": False,
    }


# ---------------------------------------------------------------- 场景构建


@dataclass
class Step:
    """say=用户语音块（transcript 为该块的 ASR final）；gap=静音。"""

    kind: str  # "say" | "gap"
    dur_s: float
    text: str = ""


def say(dur_s: float, text: str) -> Step:
    return Step("say", dur_s, text)


def gap(dur_s: float) -> Step:
    return Step("gap", dur_s)


@dataclass
class Scenario:
    kinds: list[bool] = field(default_factory=list)  # True= speech frame
    segments: list[tuple[int, int]] = field(default_factory=list)
    transcripts: list[str] = field(default_factory=list)

    @property
    def total_frames(self) -> int:
        return len(self.kinds)


def build_scenario(steps: list[Step]) -> Scenario:
    """把 say/gap 步骤展开成逐帧 speech 掩码 + VAD 段 + ASR 转写序列。"""
    sc = Scenario()
    last_say_end = None
    for st in steps:
        n = max(1, round(st.dur_s / FRAME_S))
        if st.kind == "say":
            if last_say_end is not None:
                gap_frames = len(sc.kinds) - last_say_end
                if gap_frames < round(MIN_REGION_GAP_S / FRAME_S):
                    raise ValueError(
                        f"say 间隔 {gap_frames} 帧 < ASR 断句下限：{st.text!r}"
                    )
            start = len(sc.kinds)
            sc.kinds.extend([True] * n)
            sc.segments.append((start, len(sc.kinds)))
            sc.transcripts.append(st.text)
            last_say_end = len(sc.kinds)
        else:
            sc.kinds.extend([False] * n)
    return sc


async def paced_audio(sc: Scenario):
    """按 32ms/帧定速吐帧（墙钟），单调时钟防漂移。"""
    deadline = time.monotonic()
    for is_speech in sc.kinds:
        yield tone_frame() if is_speech else silence_frame()
        deadline += FRAME_S
        delay = deadline - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            deadline = time.monotonic()
            await asyncio.sleep(0)


# ---------------------------------------------------------------- 会话执行


@dataclass
class SessionResult:
    name: str
    wall_s: float
    events: list[Any]
    records: list[Any]
    llm_requests: list[Any]
    tts: Any
    player: Any
    core: Any
    errors: list[str]
    metrics_path: str | None
    summary_path: str | None
    state_changes: list[tuple[str, str]] = field(default_factory=list)

    def ev(self, et: EventType) -> list[Any]:
        return [e for e in self.events if e.type == et]

    @property
    def closed(self) -> list[Any]:
        return [r for r in self.records if r.closed]

    @property
    def replies(self) -> list[Any]:
        return [
            e
            for e in self.ev(EventType.AGENT_REPLY)
            if e.payload.get("speech")
        ]


async def run_session(
    name: str,
    steps: list[Step],
    *,
    turn: Any = None,
    llm: Any = None,
    tts: Any = None,
    outdir: Path | None,
    playback_wav: Path | None = None,
    settle_s: float = 0.6,
) -> SessionResult:
    """脚本化 provider + 真实 pipeline 跑一个场景，收集事件/账本/历史。"""
    sc = build_scenario(steps)
    vad = ScriptedVAD(sc.segments)
    turn = turn or ScriptedTurn(release_on_final=True)
    asr = ScriptedASR(sc.transcripts, silence_frames_end=6)
    llm = llm or ScriptedLLM(
        reply_for, first_token_delay_s=0.35, token_delay_s=0.008
    )
    tts = tts or PaceTTS(sample_rate=24000)
    player = WavSinkPlayer(
        sample_rate=24000, realtime=True, out_path=playback_wav
    )
    core = ConversationCore(playback=player, tts=tts)
    metrics = LatencyLog(core.bus, outdir=outdir)
    pipeline = VoicePipeline(
        audio=paced_audio(sc),
        vad=vad,
        turn=turn,
        asr=asr,
        agent=CharacterAgent(llm),
        tts=tts,
        player=player,
        core=core,
        metrics=metrics,
        auto_stop=True,
        auto_stop_settle_s=settle_s,
    )
    t0 = time.monotonic()
    await pipeline.run()
    wall = time.monotonic() - t0
    summary_path = metrics.close()
    player.close()
    state_changes = [
        (e.payload.get("from"), e.payload.get("to"))
        for e in pipeline.bus.history
        if e.type == EventType.STATE_CHANGED
    ]
    return SessionResult(
        name=name,
        wall_s=wall,
        events=pipeline.bus.history,
        records=metrics.records,
        llm_requests=llm.requests,
        tts=tts,
        player=player,
        core=core,
        errors=list(pipeline.errors),
        metrics_path=str(metrics.path) if metrics.path else None,
        summary_path=str(summary_path) if summary_path else None,
        state_changes=state_changes,
    )


# ---------------------------------------------------------------- 用例 A–G


async def case_a(outdir: Path) -> dict[str, Any]:
    """A. 普通一问一答：正确结束 + 快速回复。"""
    r = await run_session(
        "A",
        [gap(3), say(1.5, "你觉得今天吃什么？"), gap(6)],
        outdir=outdir,
    )
    closed = r.closed
    rec = closed[0] if closed else None
    checks = {
        "one_turn_completed": len(closed) == 1
        and rec is not None
        and rec.stop_reason == "completed",
        "reply_spoken": bool(r.replies)
        and (rec.reply_speech or "") != "",
        "sefa_recorded": rec is not None
        and rec.sefa_ms is not None
        and rec.sefa_ms > 0,
        "no_errors": not r.errors,
        "state_idle_end": r.core.state == ConversationState.IDLE,
    }
    return {
        "case": "A 普通一问一答",
        "pass": all(checks.values()),
        "checks": checks,
        "sefa_ms": rec.sefa_ms if rec else None,
        "user_text": rec.user_text if rec else None,
        "reply": rec.reply_speech if rec else None,
        "speculative": rec.speculative if rec else None,
        "log": r.metrics_path,
    }


async def case_b(outdir: Path) -> dict[str, Any]:
    """B. 句中停顿：第一次停顿不抢话，整句只回一次。"""
    steps = [
        gap(3),
        say(1.3, "我觉得这个东西吧"),
        gap(0.5),  # 句中停顿：VAD 停、Smart Turn 判 incomplete
        say(1.5, "我觉得这个东西吧，嗯，其实还行"),
        gap(6),
    ]
    r = await run_session(
        "B",
        steps,
        # 第一次停顿 → INCOMPLETE 裁决；第二次停顿走 final 闸门放行。
        turn=ScriptedTurn(verdicts=[False], release_on_final=True),
        outdir=outdir,
    )
    incompletes = r.ev(EventType.TURN_INCOMPLETE)
    completes = r.ev(EventType.USER_TURN_COMPLETE)
    responds = r.ev(EventType.AGENT_CAN_RESPOND)
    rec = r.closed[0] if r.closed else None
    # 第一次 ASR final 到达时不得触发回应（抢话=第一次停顿就回复）。
    first_final_idx = next(
        (i for i, e in enumerate(r.events) if e.type == EventType.ASR_FINAL),
        None,
    )
    respond_after_first = [
        e
        for e in responds
        if first_final_idx is None
        or r.events.index(e) > first_final_idx
    ]
    checks = {
        "incomplete_on_first_pause": len(incompletes) >= 1,
        "single_turn_complete": len(completes) == 1,
        "no_early_response": len(respond_after_first) == len(responds) == 1,
        "single_llm_call": len(r.llm_requests) == 1,
        "full_text_turn": rec is not None
        and "其实还行" in (rec.user_text or ""),
        "reply_once": len(r.replies) == 1,
        "no_errors": not r.errors,
    }
    return {
        "case": "B 句中停顿",
        "pass": all(checks.values()),
        "checks": checks,
        "turn_text": rec.user_text if rec else None,
        "reply": rec.reply_speech if rec else None,
        "log": r.metrics_path,
    }


async def case_c(outdir: Path) -> dict[str, Any]:
    """C. 半句打断：立刻停 + 下一轮知道自己没说完。"""
    steps = [
        gap(3),
        say(1.5, "讲讲你自己吧"),  # 长回复（_LONG_REPLY ~7s 播放）
        gap(3.0),  # 回复中段（~0.7s 开口 + ~2.3s 播出）用户插话
        say(1.8, "你最好想清楚再说"),
        gap(8),
    ]
    r = await run_session("C", steps, outdir=outdir)
    closed = r.closed
    first = closed[0] if closed else None
    second = closed[1] if len(closed) > 1 else None
    interrupted_heard = [
        e
        for e in r.core.context.heard_history
        if e.get("role") == "assistant" and e.get("interrupted")
    ]
    last_user_msg = (
        str(r.llm_requests[-1][-1].get("content", ""))
        if len(r.llm_requests) >= 2
        else ""
    )
    checks = {
        "turn1_interrupted": first is not None
        and first.stop_reason == "interrupted",
        "interrupt_ts_recorded": first is not None
        and first.t_interrupt_detected is not None
        and first.t_playback_stopped is not None,
        "barge_in_stop_ms": first is not None
        and first.barge_in_stop_ms is not None
        and first.barge_in_stop_ms >= 0,
        "heard_truncated": bool(interrupted_heard)
        and len(interrupted_heard[0].get("text", ""))
        < len(_LONG_REPLY),
        "tts_cancelled": r.tts.cancelled_count >= 1,
        "turn2_completed": second is not None
        and second.stop_reason == "completed",
        "knows_interrupted": "assistant_was_interrupted" in last_user_msg
        and "assistant_generated_but_not_heard" in last_user_msg,
        "no_errors": not r.errors,
    }
    return {
        "case": "C 半句打断",
        "pass": all(checks.values()),
        "checks": checks,
        "barge_in_stop_ms": first.barge_in_stop_ms if first else None,
        "heard_after_interrupt": interrupted_heard[0]["text"]
        if interrupted_heard
        else None,
        "log": r.metrics_path,
    }


async def case_d(outdir: Path) -> dict[str, Any]:
    """D. 用户要求安静：SILENCED 内不回应、不主动插话；醒后恢复。"""
    steps = [
        gap(3),
        say(1.5, "你先闭嘴15秒"),
        gap(4),  # SILENCED 生效中
        say(1.2, "喂，你还在吗？"),  # 静默期内用户再说话 → 不应回应
        gap(8),  # 覆盖 15s 静默窗口尾部
        say(1.5, "派蒙，继续聊吧"),
        gap(8),
    ]
    r = await run_session("D", steps, outdir=outdir)
    silenced_entries = [
        e
        for e in r.events
        if e.type == EventType.STATE_CHANGED
        and e.payload.get("to") == ConversationState.SILENCED
    ]
    silenced_exits = [
        e
        for e in r.events
        if e.type == EventType.STATE_CHANGED
        and e.payload.get("from") == ConversationState.SILENCED
    ]
    # 静默期内的那次轮次：完成裁决但不发 AGENT_CAN_RESPOND。
    completes = r.ev(EventType.USER_TURN_COMPLETE)
    responds = r.ev(EventType.AGENT_CAN_RESPOND)
    sil_req = r.ev(EventType.SILENCE_REQUESTED)
    # SILENCED 起止区间内的 AGENT_CAN_RESPOND 计数（应=0）
    in_window_responds = 0
    if silenced_entries and silenced_exits:
        t0, t1 = silenced_entries[0].ts, silenced_exits[-1].ts
        in_window_responds = len(
            [e for e in responds if t0 <= e.ts <= t1]
        )
    in_window_initiative = 0
    if silenced_entries and silenced_exits:
        in_window_initiative = len(
            [
                e
                for e in r.ev(EventType.INITIATIVE_TRIGGERED)
                if silenced_entries[0].ts <= e.ts <= silenced_exits[-1].ts
            ]
        )
    checks = {
        "silence_requested": bool(sil_req),
        "entered_silenced": bool(silenced_entries),
        "exited_silenced": bool(silenced_exits),
        "no_respond_in_window": in_window_responds == 0,
        "no_initiative_in_window": in_window_initiative == 0,
        "turns_adjudicated": len(completes) >= 3,
        "respond_after": len(responds) >= 1
        and len(r.replies) >= 1,
        "no_errors": not r.errors,
    }
    return {
        "case": "D 用户要求安静",
        "pass": all(checks.values()),
        "checks": checks,
        "turn_completes": len(completes),
        "agent_responds": len(responds),
        "silenced_for_s": (
            round(silenced_exits[-1].ts - silenced_entries[0].ts, 2)
            if silenced_entries and silenced_exits
            else None
        ),
        "log": r.metrics_path,
    }


async def case_e(outdir: Path) -> dict[str, Any]:
    """E. 长时间沉默：InitiativePolicy 在合理窗口内可能开口、不过频。"""
    r = await run_session(
        "E",
        [gap(4), say(1.5, "嗯，我想想"), gap(6), gap(95)],
        outdir=outdir,
        settle_s=1.0,
    )
    triggers = r.ev(EventType.INITIATIVE_TRIGGERED)
    ts = [e.ts for e in triggers]
    gaps = [round(b - a, 1) for a, b in zip(ts, ts[1:])]
    checks = {
        "initiative_fired": 1 <= len(triggers) <= 3,  # ~95s → 预期 1–2 次
        "not_too_frequent": all(g >= 40 for g in gaps),
        "bounded_llm_calls": len(r.llm_requests)
        <= len(triggers) + 1,
        "no_errors": not r.errors,
    }
    return {
        "case": "E 长时间沉默",
        "pass": all(checks.values()),
        "checks": checks,
        "initiative_triggers": len(triggers),
        "trigger_reasons": [
            e.payload.get("reason") for e in triggers
        ],
        "trigger_gaps_s": gaps,
        "log": r.metrics_path,
    }


async def case_f(outdir: Path) -> dict[str, Any]:
    """F. 用户连续讲话 ~45s：派蒙不找机会抢话。"""
    mono = "我今天想跟你聊一个比较长的事情" * 12
    r = await run_session(
        "F",
        [gap(3), say(45, mono), gap(8)],
        outdir=outdir,
    )
    speech_start = r.ev(EventType.USER_SPEECH_STARTED)[0].ts
    completes = r.ev(EventType.USER_TURN_COMPLETE)
    turn_end = completes[0].ts if completes else None
    during = [
        e
        for e in r.events
        if e.type in (EventType.AGENT_CAN_RESPOND, EventType.AGENT_SPEAKING)
        and speech_start <= e.ts <= (turn_end or speech_start)
    ]
    checks = {
        "no_response_during_speech": not during,
        "single_turn": len(completes) == 1,
        "single_reply": len(r.replies) == 1,
        "no_initiative_during": not [
            e
            for e in r.ev(EventType.INITIATIVE_TRIGGERED)
            if e.ts >= speech_start
            and e.ts <= (turn_end or speech_start)
        ],
        "no_errors": not r.errors,
    }
    return {
        "case": "F 用户连续讲话",
        "pass": all(checks.values()),
        "checks": checks,
        "speech_len_s": 45,
        "log": r.metrics_path,
    }


async def case_g(outdir: Path) -> dict[str, Any]:
    """G. 连续互怼：快速短句的延迟/中断/短上下文一致性。"""
    steps = [gap(3)]
    for line in ("你烦不烦？", "你才烦。", "你再说？", "说就说。"):
        steps += [say(0.9, line), gap(4.5)]
    r = await run_session("G", steps, outdir=outdir)
    closed = [x for x in r.closed if x.stop_reason in ("completed", "noop")]
    heard = r.core.context.heard_history
    sefas = [x.sefa_ms for x in closed if x.sefa_ms is not None]
    # 短上下文一致性：heard 历史 user/assistant 交替、4 轮用户文本全在。
    user_entries = [e for e in heard if e.get("role") == "user"]
    checks = {
        "four_turns": len(closed) >= 4,
        "all_completed": all(
            x.stop_reason == "completed" for x in closed[:4]
        ),
        "sefa_each_turn": len(sefas) >= 4,
        "context_consistent": len(user_entries) >= 4
        and user_entries[-1].get("text") == "说就说。",
        "no_errors": not r.errors,
    }
    return {
        "case": "G 连续互怼",
        "pass": all(checks.values()),
        "checks": checks,
        "turns": len(closed),
        "sefa_ms": [round(s, 1) for s in sefas],
        "replies": [
            x.reply_speech for x in closed[:4]
        ],
        "log": r.metrics_path,
    }


# ---------------------------------------------------------------- 10min soak


def soak_steps(minutes: float) -> list[Step]:
    """≥minutes 分钟连续对话剧本：嵌入 A–G 行为 + 填充问答轮。"""
    steps: list[Step] = [
        gap(4),
        # —— A 普通一问一答
        say(1.5, "你觉得今天吃什么？"),
        gap(7),
        # —— B 句中停顿不抢话
        say(1.3, "我觉得这个东西吧"),
        gap(0.5),
        say(1.5, "我觉得这个东西吧，嗯，其实还行"),
        gap(7),
        # —— C 长回复被半句打断
        say(1.5, "讲讲你自己吧"),
        gap(3.0),
        say(1.8, "你最好想清楚再说"),
        gap(9),
        # —— D 要求安静 → SILENCED（2 分钟窗口）→ 直呼派蒙提前唤醒
        say(1.8, "你先闭嘴两分钟"),
        gap(20),
        say(1.2, "喂，你还在吗？"),
        gap(22),
        say(1.5, "派蒙，醒醒"),
        gap(9),
        # —— E 长沉默 → InitiativePolicy 主动开口（受 cooldown 约束）
        gap(100),
        # —— F 连续讲话 45s 不抢话
        say(45, "我今天想跟你聊一个比较长的事情" * 12),
        gap(8),
    ]
    # —— G 连续互怼 ×4
    for line in ("你烦不烦？", "你才烦。", "你再说？", "说就说。"):
        steps += [say(0.9, line), gap(4.5)]
    # —— 填充轮次到目标时长：普通问答 + 偶尔停顿句
    filler = [
        "今天天气怎么样？",
        "你会做饭吗？",
        "提瓦特哪里最好玩？",
        "派蒙你多重啊？",
        "推荐一道菜吧。",
        "你会唱歌吗？",
        "旅行者最近去哪了？",
        "你最喜欢什么食物？",
        "给我讲个故事吧。",
        "你怕不怕黑？",
    ]
    i = 0
    while (
        sum(s.dur_s for s in steps) < minutes * 60 - 15
    ):  # 尾部留 15s 收尾
        steps += [say(1.6, filler[i % len(filler)]), gap(26)]
        i += 1
    steps += [gap(10)]
    return steps


async def run_soak(minutes: float) -> dict[str, Any]:
    steps = soak_steps(minutes)
    r = await run_session(
        "soak",
        steps,
        outdir=ROOT / "data" / "latency_log",
        playback_wav=SOAK_WAV,
        settle_s=1.0,
    )
    closed = r.closed
    sefas = [x.sefa_ms for x in closed if x.sefa_ms is not None]
    barges = [
        x.barge_in_stop_ms
        for x in closed
        if x.barge_in_stop_ms is not None
    ]
    interrupts = [x for x in closed if x.stop_reason == "interrupted"]
    triggers = r.ev(EventType.INITIATIVE_TRIGGERED)
    silenced = [
        e
        for e in r.events
        if e.type == EventType.STATE_CHANGED
        and e.payload.get("to") == ConversationState.SILENCED
    ]
    exits = [
        e
        for e in r.events
        if e.type == EventType.STATE_CHANGED
        and e.payload.get("from") == ConversationState.SILENCED
    ]
    in_window_responds = 0
    if silenced and exits:
        in_window_responds = len(
            [
                e
                for e in r.ev(EventType.AGENT_CAN_RESPOND)
                if silenced[0].ts <= e.ts <= exits[-1].ts
            ]
        )
    checks = {
        "ran_over_10min": r.wall_s >= 600,
        "no_pipeline_errors": not r.errors,
        "turns_recorded": len(closed) >= 15,
        "barge_in_worked": bool(interrupts)
        and all(
            x.barge_in_stop_ms is not None for x in interrupts
        ),
        "silenced_no_respond": in_window_responds == 0
        and bool(silenced),
        "initiative_bounded": 1 <= len(triggers) <= 4,
        "context_alive": len(r.core.context.heard_history) >= 10,
        "latency_log_written": r.metrics_path is not None
        and Path(r.metrics_path).exists()
        and r.summary_path is not None,
        "state_idle_end": r.core.state == ConversationState.IDLE,
    }
    return {
        "case": "SOAK 连续会话 ≥10min",
        "pass": all(checks.values()),
        "checks": checks,
        "wall_s": round(r.wall_s, 1),
        "turns": len(closed),
        "sefa_ms_p50": sorted(sefas)[len(sefas) // 2]
        if sefas
        else None,
        "sefa_ms_max": round(max(sefas), 1) if sefas else None,
        "barge_in_stop_ms": [round(b, 1) for b in barges],
        "initiative_triggers": len(triggers),
        "interrupted_turns": len(interrupts),
        "playback_wav": str(SOAK_WAV),
        "log": r.metrics_path,
        "summary": r.summary_path,
    }


# ---------------------------------------------------------------- 入口


async def _main(args) -> int:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    case_outdir = RESULTS_DIR / "latency"
    results: list[dict[str, Any]] = []

    if args.cases or args.all:
        for fn in (case_a, case_b, case_c, case_d, case_e, case_f, case_g):
            t0 = time.monotonic()
            res = await fn(case_outdir)
            res["wall_s"] = round(time.monotonic() - t0, 1)
            results.append(res)
            mark = "PASS" if res["pass"] else "FAIL"
            print(f"[case {res['case']}] {mark} ({res['wall_s']}s)")
            for k, v in res["checks"].items():
                if not v:
                    print(f"    ✗ {k}")

    if args.soak or args.all:
        print(f"[soak] {args.minutes}min continuous session …")
        res = await run_soak(args.minutes)
        results.append(res)
        mark = "PASS" if res["pass"] else "FAIL"
        print(f"[soak] {mark} wall={res['wall_s']}s turns={res['turns']}")
        for k, v in res["checks"].items():
            if not v:
                print(f"    ✗ {k}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS_DIR / f"results_{stamp}.json"
    out.write_text(
        json.dumps(
            {
                "run_at": stamp,
                "providers": "scripted (simulated.py)",
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[results] → {out}")
    return 0 if all(r["pass"] for r in results) else 1


def main() -> None:
    p = argparse.ArgumentParser(prog="mvp_acceptance")
    p.add_argument("--cases", action="store_true", help="只跑用例 A–G")
    p.add_argument("--soak", action="store_true", help="只跑 10min soak")
    p.add_argument("--all", action="store_true", help="用例 + soak（默认）")
    p.add_argument("--minutes", type=float, default=10.5)
    args = p.parse_args()
    if not (args.cases or args.soak):
        args.all = True
    try:
        raise SystemExit(asyncio.run(_main(args)))
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
