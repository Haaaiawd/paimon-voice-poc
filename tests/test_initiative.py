"""TASK-011：InitiativePolicy 评分公式与硬规则（doc 03 §2.3）。

fake clock 驱动 EventBus/StateMachine/Policy，逐项验证公式贡献与
NEVER_SPEAK 硬闸口；另有两个 pipeline 级用例验证"initiative 触发
只允许一次 LLM 调用，LLM 可返回 NOOP"。
"""

from __future__ import annotations

import asyncio

import pytest

from character.agent import CharacterAgent
from conversation.core import ConversationCore
from conversation.events import EventBus, EventType
from conversation.initiative import InitiativeConfig
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

CFG = InitiativeConfig(
    threshold=1.0,
    min_silence_s=2.0,
    silence_full_s=10.0,
    cooldown_s=30.0,
    w_recently_spoke=0.8,
    recently_spoke_decay_s=20.0,
    mention_window_s=15.0,
    energy_window_s=60.0,
    energy_full_turns=3,
)


def make_core(cfg: InitiativeConfig = CFG):
    """fake-clock ConversationCore；返回 (时钟盒, core, 触发事件列表)。"""
    t = [1000.0]
    now = lambda: t[0]  # noqa: E731
    core = ConversationCore(
        bus=EventBus(now_fn=now),
        initiative_config=cfg,
        now_fn=now,
    )
    triggered = []
    core.bus.subscribe(EventType.INITIATIVE_TRIGGERED, triggered.append)
    return t, core, triggered


# ---------------------------------------------------------------- 评分公式


def test_min_silence_floor() -> None:
    """低于 min_silence_s 的留白永远不开口（D-008 不抢话头）。"""
    t, core, _ = make_core()
    t[0] += 1.0  # < min_silence_s=2
    assert core.initiative.gate_reason(t[0]) == "min_silence"
    result = core.initiative.evaluate(t[0])
    assert result["gate"] == "min_silence"
    assert result["score"] is None


def test_silence_score_triggers_once_then_cooldown() -> None:
    """冷场拿满 silence 权重 = threshold → 触发一次；cooldown 内不重发。"""
    t, core, triggered = make_core()
    t[0] += 11.0  # silence >= silence_full_s → silence term = 1.0 ≥ threshold
    result = core.initiative.evaluate(t[0])
    assert result["gate"] is None
    assert result["score"] == pytest.approx(1.0)
    assert result["breakdown"]["silence_duration"] == pytest.approx(1.0)
    core.tick(t[0])
    assert len(triggered) == 1
    ev = triggered[0]
    assert ev.payload["reason"] == "silence_duration"
    assert ev.payload["score"] == pytest.approx(1.0)
    assert ev.payload["silence_ms"] > 10_000
    core.tick(t[0])  # cooldown 内不再触发
    assert len(triggered) == 1
    assert core.initiative.gate_reason(t[0]) == "cooldown"


def test_cooldown_expiry_retriggers() -> None:
    """cooldown 过后、沉默重新累计满 → 可再次触发。"""
    t, core, triggered = make_core()
    t[0] += 11.0
    core.tick(t[0])
    assert len(triggered) == 1
    # 触发后 last_activity 复位到触发时刻；cooldown 结束时沉默已重新涨满
    t[0] += CFG.cooldown_s + 1
    assert core.initiative.gate_reason(t[0]) is None
    core.tick(t[0])
    assert len(triggered) == 2


def test_recently_spoke_penalty() -> None:
    """派蒙刚说完 → recently_spoke 负项显著压分。"""
    t, core, _ = make_core()
    t[0] += 11.0
    core.bus.publish(EventType.AGENT_SPEAKING, {})
    core.bus.publish(EventType.PLAYBACK_STOPPED, {"reason": "finished"})
    t[0] += 10.0  # 距说完 10s < decay 20s → 负项 = 0.8*(1-0.5) = 0.4
    result = core.initiative.evaluate(t[0])
    # silence term 满 1.0，被 recently_spoke 拉回 0.6 < threshold
    assert result["breakdown"]["recently_spoke"] == pytest.approx(-0.4)
    assert result["score"] == pytest.approx(0.6)


def test_mention_and_interesting_boost() -> None:
    """直呼"派蒙" + 被打断的话头 → mention/interesting 正项加速触发。"""
    t, core, _ = make_core()
    t[0] += CFG.min_silence_s
    # "派蒙你看这个" 这一轮走完（轮次开着时 gate 本就是 user_is_speaking）
    core.bus.publish(EventType.ASR_FINAL, {"text": "派蒙你看这个"})
    core.bus.publish(EventType.TURN_COMPLETE, {"source": "model"})
    core.bus.publish(EventType.AGENT_INTERRUPTED, {})
    core.bus.publish(EventType.PLAYBACK_STOPPED, {"reason": "finished"})
    assert core.state is ConversationState.IDLE
    t[0] += 3.0  # 过 min_silence；mention 仍在 15s 窗内
    result = core.initiative.evaluate(t[0])
    assert result["gate"] is None
    assert result["breakdown"]["direct_mention"] == pytest.approx(0.6)
    assert result["breakdown"]["interesting_context"] == pytest.approx(0.4)
    assert result["score"] >= 1.0  # 0.3 silence + 0.6 + 0.4 ≥ threshold


def test_conversation_energy() -> None:
    """近期轮次密度 → energy 正项（3 轮打满 energy_full_turns）。"""
    t, core, _ = make_core()
    for _ in range(3):
        core.bus.publish(EventType.USER_SPEECH_STARTED)
        core.bus.publish(EventType.TURN_COMPLETE, {"source": "model"})
        core.bus.publish(EventType.PLAYBACK_STOPPED, {"reason": "finished"})
        assert core.state is ConversationState.IDLE
    t[0] += CFG.min_silence_s + 1
    result = core.initiative.evaluate(t[0])
    assert result["breakdown"]["conversation_energy"] == pytest.approx(0.3)


# ---------------------------------------------------------------- 硬规则


def test_hard_gate_user_is_speaking() -> None:
    """§2.3 硬规则：user_is_speaking → NEVER_SPEAK（含裁决在途的 POSSIBLE_END）。"""
    t, core, triggered = make_core()
    t[0] += 60.0  # 沉默充分
    core.bus.publish(EventType.USER_SPEECH_STARTED)
    assert core.initiative.gate_reason(t[0]) == "user_is_speaking"
    core.bus.publish(EventType.USER_SPEECH_STOPPED)
    # POSSIBLE_END + turn_open：轮次还在裁决，仍算用户在说
    assert core.initiative.gate_reason(t[0]) == "user_is_speaking"
    core.bus.publish(EventType.TURN_COMPLETE, {"source": "model"})
    # 确认轮 → THINKING：响应在途，不叠加主动开口
    assert core.initiative.gate_reason(t[0]) == "response_in_flight"
    core.bus.publish(EventType.PLAYBACK_STOPPED, {"reason": "finished"})
    assert core.state is ConversationState.IDLE
    core.tick(t[0])
    assert triggered == []  # 这些事件刷新了沉默基准，未到 min_silence


def test_hard_gate_silenced() -> None:
    """§2.3 硬规则：SILENCED → NEVER_SPEAK。"""
    t, core, triggered = make_core()
    t[0] += 60.0
    core.bus.publish(EventType.SILENCE_REQUESTED, {"text": "闭嘴"})
    assert core.state is ConversationState.SILENCED
    assert core.initiative.gate_reason(t[0]) == "silenced"
    core.tick(t[0])
    assert triggered == []
    assert core.machine._silenced_until is not None


def test_hard_gate_speaking() -> None:
    """SPEAKING 中不叠加第二次主动开口（在途响应面）。"""
    t, core, _ = make_core()
    t[0] += 60.0
    core.bus.publish(EventType.AGENT_SPEAKING, {})
    assert core.state is ConversationState.SPEAKING
    assert core.initiative.gate_reason(t[0]) == "response_in_flight"


# ---------------------------------------------------------------- pipeline 级


def _is_initiative_request(messages: list[dict]) -> bool:
    return "initiative_reason" in str(messages[-1].get("content", ""))


def _frames() -> list[bytes]:
    return (
        [silence_frame() for _ in range(2)]
        + [tone_frame() for _ in range(26)]
        + [silence_frame() for _ in range(20)]
    )


def _initiative_pipeline(llm: ScriptedLLM, tts: ToneTTS) -> VoicePipeline:
    """用户轮 + 快速 initiative 配置的全脚本化 pipeline。"""
    player = WavSinkPlayer(sample_rate=24000, realtime=False)
    core = ConversationCore(
        playback=player,
        tts=tts,
        initiative_config=InitiativeConfig(
            threshold=0.05,
            min_silence_s=0.2,
            silence_full_s=0.4,
            cooldown_s=30.0,
        ),
    )
    metrics = LatencyLog(core.bus)
    return VoicePipeline(
        audio=list_frames(_frames()),
        vad=ScriptedVAD([(2, 28)]),
        turn=ScriptedTurn(),
        asr=ScriptedASR("你好"),
        agent=CharacterAgent(llm),
        tts=tts,
        player=player,
        core=core,
        metrics=metrics,
        auto_stop=True,
        auto_stop_settle_s=1.0,
    )


@pytest.mark.asyncio
async def test_pipeline_initiative_response() -> None:
    """initiative 触发 → 允许一次 LLM 调用 → 走正常说话路径出声。"""
    tts = ToneTTS(sample_rate=24000, secs_per_chunk=0.05)
    llm = ScriptedLLM(
        reply=lambda messages: {
            "speech": (
                "喂，你怎么不理派蒙！"
                if _is_initiative_request(messages)
                else "哈？"
            ),
            "emotion": "happy",
            "energy": 0.7,
            "should_continue": False,
        }
    )
    pipeline = _initiative_pipeline(llm, tts)
    await asyncio.wait_for(pipeline.run(), timeout=15)

    assert not pipeline.errors
    initiative_calls = [
        m for m in llm.requests if _is_initiative_request(m)
    ]
    assert len(initiative_calls) == 1  # 触发只放行一次调用
    assert len(tts.synthesized) >= 2  # 用户轮回复 + initiative 回复
    assert pipeline.core.state is ConversationState.IDLE


@pytest.mark.asyncio
async def test_pipeline_initiative_noop() -> None:
    """initiative 触发后 LLM 返回空 speech → NOOP：不合成不出声。"""
    tts = ToneTTS(sample_rate=24000, secs_per_chunk=0.05)
    llm = ScriptedLLM(
        reply=lambda messages: {
            "speech": "" if _is_initiative_request(messages) else "哈？",
            "emotion": "neutral",
            "energy": 0.5,
            "should_continue": False,
        }
    )
    pipeline = _initiative_pipeline(llm, tts)
    await asyncio.wait_for(pipeline.run(), timeout=15)

    assert not pipeline.errors
    initiative_calls = [
        m for m in llm.requests if _is_initiative_request(m)
    ]
    assert len(initiative_calls) == 1  # 触发允许调一次
    assert len(tts.synthesized) == 1  # 只有用户轮回复真正出声
    replies = [
        e for e in pipeline.bus.history if e.type is EventType.AGENT_REPLY
    ]
    noop_replies = [e for e in replies if e.payload.get("noop")]
    assert len(noop_replies) == 1  # initiative 轮的 NOOP 被如实上报
    # 空 utterance 不入 heard history（没有"派蒙说过"的空内容）
    heard = pipeline.core.context.heard_history
    assert all(e["text"] for e in heard if e["role"] == "assistant")
