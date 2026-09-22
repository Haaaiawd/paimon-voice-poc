"""TASK-011：SILENCED 语义——"闭嘴"进静默，"派蒙"唤醒，超时兜底。

规则面（无 LLM）：
- SilenceClassifier 消费 ASR_FINAL，命中静默指令 → SILENCE_REQUESTED →
  全局迁移进 SILENCED；时长短语（"两分钟"/"30秒"）覆盖默认窗口；
- SILENCED 中 ASR 继续转写、轮次照常裁决但不发 AGENT_CAN_RESPOND
  （TurnManager 挡在状态机层）；final 直呼"派蒙" → wake() 回 IDLE；
- tick 到窗口边界 → silence_timeout 自动回 IDLE。

集成用例走真实 VoicePipeline：三段语音"问一句 → 闭嘴 → 派蒙唤醒"，
断言静默窗内零主动开口、零 LLM 响应，唤醒轮正常回复。
"""

from __future__ import annotations

import asyncio

import pytest

from character.agent import CharacterAgent
from conversation.core import ConversationCore
from conversation.events import EventBus, EventType
from conversation.initiative import (
    InitiativeConfig,
    parse_silence_duration,
)
from conversation.state_machine import ConversationState
from metrics.latency import LatencyLog
from runtime.pipeline import VoicePipeline
from runtime.simulated import (
    ScriptedASR,
    ScriptedLLM,
    ScriptedTurn,
    ScriptedVAD,
    ToneTTS,
    WavSinkPlayer,
    list_frames,
    silence_frame,
    tone_frame,
)


def make_core(**kwargs):
    t = [1000.0]
    now = lambda: t[0]  # noqa: E731
    core = ConversationCore(bus=EventBus(now_fn=now), now_fn=now, **kwargs)
    return t, core


def final(core, text: str) -> None:
    core.bus.publish(EventType.ASR_FINAL, {"text": text})


# ---------------------------------------------------------------- 时长解析


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("你闭嘴两分钟", 120.0),
        ("安静30秒", 30.0),
        ("半个小时别说话", 1800.0),
        ("闭嘴十分钟", 600.0),
        ("先别说五分钟话", 300.0),
        ("闭嘴", None),
        ("安静一下", None),
    ],
)
def test_parse_silence_duration(text: str, expected: float | None) -> None:
    assert parse_silence_duration(text) == expected


# ---------------------------------------------------------------- 指令识别


@pytest.mark.parametrize(
    "text",
    ["你闭嘴", "别说话", "安静一下", "先别说了", "别吵了", "不用说话了"],
)
def test_silence_command_recognized(text: str) -> None:
    _, core = make_core()
    assert core.silence_rules.is_silence_command(text)


@pytest.mark.parametrize(
    "text",
    [
        "你觉得今天吃什么",
        "今天很安静",
        "派蒙别闭嘴",  # 反指令：让派蒙继续说
        "不要闭嘴",
        "别住嘴",
    ],
)
def test_non_command_not_recognized(text: str) -> None:
    _, core = make_core()
    assert not core.silence_rules.is_silence_command(text)


def test_silence_command_is_rule_based_no_llm() -> None:
    """分类器是纯规则：整个 core 构造不持任何 LLM 依赖。"""
    _, core = make_core()
    final(core, "你闭嘴")
    events = [
        e for e in core.bus.history if e.type is EventType.SILENCE_REQUESTED
    ]
    assert len(events) == 1
    assert events[0].payload["text"] == "你闭嘴"


# ---------------------------------------------------------------- 状态语义


def test_silence_request_enters_silenced() -> None:
    _, core = make_core()
    final(core, "你闭嘴")
    assert core.state is ConversationState.SILENCED


def test_explicit_duration_overrides_default_window() -> None:
    """"闭嘴两分钟" → duration_s=120 覆盖默认 silence_window。"""
    t, core = make_core(silence_window_s=60.0)
    final(core, "你闭嘴两分钟")
    assert core.state is ConversationState.SILENCED
    assert core.machine._silenced_until == pytest.approx(t[0] + 120.0)
    t[0] += 119.0
    core.tick(t[0])
    assert core.state is ConversationState.SILENCED
    t[0] += 2.0  # >120s
    core.tick(t[0])
    assert core.state is ConversationState.IDLE


def test_timeout_exit_without_duration() -> None:
    """无显式时长 → 默认 silence_window 超时回 IDLE。"""
    t, core = make_core(silence_window_s=60.0)
    final(core, "别说话")
    assert core.state is ConversationState.SILENCED
    t[0] += 59.0
    core.tick(t[0])
    assert core.state is ConversationState.SILENCED
    t[0] += 2.0
    core.tick(t[0])
    assert core.state is ConversationState.IDLE
    changed = [
        e for e in core.bus.history if e.type is EventType.STATE_CHANGED
    ]
    assert changed[-1].payload["trigger"] == "silence_timeout"


def test_wake_word_exits_silenced() -> None:
    """SILENCED 中 final 直呼"派蒙" → wake 回 IDLE。"""
    _, core = make_core()
    final(core, "你闭嘴")
    assert core.state is ConversationState.SILENCED
    final(core, "派蒙起床啦")
    assert core.state is ConversationState.IDLE
    changed = [
        e for e in core.bus.history if e.type is EventType.STATE_CHANGED
    ]
    assert changed[-1].payload["trigger"] == "wake"


def test_non_wake_text_keeps_silenced() -> None:
    """静默期普通说话不解除静默（ASR 继续但派蒙不醒）。"""
    _, core = make_core()
    final(core, "你闭嘴")
    final(core, "我在自言自语")
    assert core.state is ConversationState.SILENCED


def test_silence_command_in_silenced_stays() -> None:
    """静默期重申"别说话"：保持沉默，且"派蒙别说话"不误唤醒。"""
    _, core = make_core()
    final(core, "你闭嘴")
    final(core, "派蒙先别说话")  # 含唤醒词但是静默指令 → 不醒
    assert core.state is ConversationState.SILENCED


def test_asr_continues_while_silenced_no_can_respond() -> None:
    """SILENCED 中 ASR 转写照常、轮次裁决照常，但不发 AGENT_CAN_RESPOND。"""
    _, core = make_core()
    final(core, "你闭嘴")
    assert core.state is ConversationState.SILENCED
    core.bus.publish(EventType.USER_SPEECH_STARTED)
    final(core, "今天天气怎么样")
    core.bus.publish(EventType.TURN_COMPLETE, {"source": "model"})
    completes = [
        e
        for e in core.bus.history
        if e.type is EventType.USER_TURN_COMPLETE
    ]
    assert len(completes) == 1  # 轮次正常裁决（ASR 继续的证据）
    can_respond = [
        e
        for e in core.bus.history
        if e.type is EventType.AGENT_CAN_RESPOND
    ]
    assert can_respond == []  # 但绝不放行进 LLM
    assert core.state is ConversationState.SILENCED


def test_initiative_never_fires_while_silenced() -> None:
    """硬规则 e2e：快触发配置下 SILENCED 窗内仍零主动开口。"""
    t, core = make_core(
        initiative_config=InitiativeConfig(
            threshold=0.01,
            min_silence_s=1.0,
            silence_full_s=2.0,
            cooldown_s=0.0,
        ),
        silence_window_s=10.0,
    )
    triggered = []
    core.bus.subscribe(EventType.INITIATIVE_TRIGGERED, triggered.append)
    final(core, "你闭嘴")
    t[0] += 5.0
    for _ in range(5):
        core.tick(t[0])
        t[0] += 1.0
    assert triggered == []
    assert core.state is ConversationState.SILENCED


# ---------------------------------------------------------------- 集成 e2e


@pytest.mark.asyncio
async def test_e2e_silence_then_wake() -> None:
    """三段语音：问一句（回复）→ "你闭嘴"（静默）→ "派蒙…"（唤醒回复）。

    验证验收标准 3：闭嘴进 SILENCED、窗内零主动开口、派蒙直呼唤醒。
    """
    frames = (
        [silence_frame() for _ in range(2)]
        + [tone_frame() for _ in range(13)]  # 轮1：问一句
        + [silence_frame() for _ in range(25)]
        + [tone_frame() for _ in range(13)]  # 轮2："你闭嘴"
        + [silence_frame() for _ in range(25)]
        + [tone_frame() for _ in range(13)]  # 轮3："派蒙起床啦"
        + [silence_frame() for _ in range(20)]
    )
    player = WavSinkPlayer(sample_rate=24000, realtime=False)
    tts = ToneTTS(sample_rate=24000, secs_per_chunk=0.05)
    llm = ScriptedLLM(
        {
            "speech": "哈？你现在才发现？",
            "emotion": "smug",
            "energy": 0.7,
            "should_continue": False,
        }
    )
    core = ConversationCore(
        playback=player,
        tts=tts,
        silence_window_s=30.0,
        # 快触发档：若 SILENCED 闸口失效，窗内必然看到 INITIATIVE_TRIGGERED
        initiative_config=InitiativeConfig(
            threshold=0.01,
            min_silence_s=0.05,
            silence_full_s=0.1,
            cooldown_s=0.0,
        ),
    )
    metrics = LatencyLog(core.bus)
    pipeline = VoicePipeline(
        audio=list_frames(frames),
        vad=ScriptedVAD([(2, 15), (40, 53), (78, 91)]),
        turn=ScriptedTurn(release_on_final=True),
        asr=ScriptedASR(
            ["你觉得今天吃什么", "你闭嘴", "派蒙起床啦"],
            partial_every=1,
            chars_per_partial=99,
        ),
        agent=CharacterAgent(llm),
        tts=tts,
        player=player,
        core=core,
        metrics=metrics,
        auto_stop=True,
        auto_stop_settle_s=0.5,
    )
    await asyncio.wait_for(pipeline.run(), timeout=15)

    assert not pipeline.errors

    # SILENCE_REQUESTED 由规则分类器发出（无 LLM 参与）
    silence_reqs = [
        e
        for e in pipeline.bus.history
        if e.type is EventType.SILENCE_REQUESTED
    ]
    assert len(silence_reqs) == 1
    assert silence_reqs[0].payload["text"] == "你闭嘴"

    # 状态轨迹：进过 SILENCED，且由 wake 退出（不是超时）
    changes = [
        e.payload
        for e in pipeline.bus.history
        if e.type is EventType.STATE_CHANGED
    ]
    assert any(c["to"] == "SILENCED" for c in changes)
    assert any(
        c["from"] == "SILENCED" and c["trigger"] == "wake" for c in changes
    )
    assert pipeline.core.state is ConversationState.IDLE

    # "闭嘴"轮照常裁决但不放行 LLM；唤醒轮放行 → 正常 LLM 请求恰两次
    completes = [
        e
        for e in pipeline.bus.history
        if e.type is EventType.USER_TURN_COMPLETE
    ]
    can_respond = [
        e
        for e in pipeline.bus.history
        if e.type is EventType.AGENT_CAN_RESPOND
    ]
    assert len(completes) == 3
    assert len(can_respond) == 2
    normal_calls = [
        m
        for m in llm.requests
        if "initiative_reason" not in str(m[-1].get("content", ""))
    ]
    assert len(normal_calls) == 2

    # 静默窗内零主动开口：所有 INITIATIVE_TRIGGERED 必须晚于 wake 退出时刻
    wake_ts = next(
        e.ts
        for e in pipeline.bus.history
        if e.type is EventType.STATE_CHANGED
        and e.payload["from"] == "SILENCED"
    )
    during_silence = [
        e
        for e in pipeline.bus.history
        if e.type is EventType.INITIATIVE_TRIGGERED and e.ts < wake_ts
    ]
    assert during_silence == []
