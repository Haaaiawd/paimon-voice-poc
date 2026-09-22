"""TASK-006 acceptance：打断六步 + heard history 双历史。

verify_by:
1. pytest tests/test_interruption.py 断言六步全部发生（停播/清 TTS buffer/
   取消 LLM/记录已播/INTERRUPTED→LISTENING）；
2. 断言构造的 context payload 结构与 doc 03 §3 一致
   （assistant_heard / assistant_generated_but_not_heard /
   assistant_was_interrupted）。

边界（task）：假音频/假 LLM 驱动，不接真实 provider。
"""

from __future__ import annotations

from conversation import (
    ASSISTANT_INTERRUPTED_EVENT,
    ConversationCore,
    ConversationState,
    EventType,
)

S = ConversationState
E = EventType

#: doc 03 §3 例句与其分段（两颗 TTS 文本块，时长可算出 heard 边界）。
GENERATED = "我觉得你今天这个发型特别像一只刚睡醒的史莱姆。"
SEG_HEARD = "我觉得你今天这个发型特别像"
SEG_UNHEARD = "一只刚睡醒的史莱姆。"


class FakePlayback:
    """假播放器：stop() 记录调用、清 buffer、返回预设已播秒数。

    与 runtime.playback.StreamingPlayer.stop() 契约一致。
    """

    def __init__(self, played_s: float = 0.0) -> None:
        self.played_s = played_s
        self.stop_calls = 0
        self.buffer: list[bytes] = [b"unplayed-pcm"]

    def stop(self) -> float:
        self.stop_calls += 1
        self.buffer.clear()
        return self.played_s


class FakeTTS:
    """假 TTS：cancel() 为协程（贴合 TTSProvider 契约），清本地 buffer。"""

    def __init__(self) -> None:
        self.cancel_calls = 0
        self.buffer: list[str] = ["queued-text-chunk"]

    async def cancel(self) -> None:
        self.cancel_calls += 1
        self.buffer.clear()


class FakeLLM:
    """假在途 LLM：cancel() 置位（pipeline 侧等价于 asyncio.Task.cancel）。"""

    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


def make_core(played_s: float = 0.0, **kwargs):
    playback = FakePlayback(played_s)
    tts = FakeTTS()
    llm = FakeLLM()
    core = ConversationCore(playback=playback, tts=tts, **kwargs)
    core.interruption.track_llm(llm.cancel)
    return core, playback, tts, llm


def drive_to_thinking(core: ConversationCore) -> None:
    """干净用户轮次 → THINKING（LLM 可以开始生成）。"""
    core.publish(E.USER_SPEECH_STARTED)
    core.publish(E.ASR_FINAL, {"text": "派蒙你看我新发型"})
    core.publish(E.USER_SPEECH_STOPPED)
    core.publish(E.TURN_COMPLETE, {"source": "model"})
    assert core.state == S.THINKING


def drive_to_speaking(core: ConversationCore) -> None:
    drive_to_thinking(core)
    core.publish(E.AGENT_SPEAKING)
    assert core.state == S.SPEAKING


def doc_utterance(core: ConversationCore):
    """doc 03 §3 例句：全句 generated + 两段已交付音频（1.3s + 0.7s）。"""
    utterance = core.context.begin_utterance()
    utterance.add_generated(GENERATED)
    utterance.add_segment(SEG_HEARD, 1.3)
    utterance.add_segment(SEG_UNHEARD, 0.7)
    return utterance


def state_transitions(core: ConversationCore) -> list[tuple[str, str]]:
    return [
        (e.payload["from"], e.payload["to"])
        for e in core.bus.history
        if e.type == E.STATE_CHANGED
    ]


class TestInterruptionSixSteps:
    def test_barge_in_executes_all_six_steps(self):
        """doc 03 §2.2：SPEAKING 中 USER_SPEECH_STARTED → 六步全发生。"""
        core, playback, tts, llm = make_core(played_s=1.3)
        drive_to_speaking(core)
        utterance = doc_utterance(core)
        core.bus.clear_history()

        core.publish(E.USER_SPEECH_STARTED)  # 用户插嘴

        # 1. 停止音频播放：stop 被调且拿到实际已播秒数
        assert playback.stop_calls == 1
        assert playback.buffer == []
        # 2. 清空未播放 TTS buffer：cancel 协程被实际执行
        assert tts.cancel_calls == 1
        assert tts.buffer == []
        # 3. 取消在途 LLM
        assert llm.cancelled is True
        assert core.interruption.llm_in_flight is False
        # 4. 记录已播放内容：utterance 按 played_s 截断封盘
        assert utterance.interrupted is True
        assert utterance.open is False
        assert utterance.played_s == 1.3
        assert utterance.heard == SEG_HEARD
        assert utterance.not_heard == SEG_UNHEARD
        # 5. 状态切为 INTERRUPTED（同一触发事件上状态机完成迁移）
        transitions = state_transitions(core)
        assert ("SPEAKING", "INTERRUPTED") in transitions
        # 6. 重新进入 LISTENING（PLAYBACK_STOPPED 驱动）
        assert ("INTERRUPTED", "LISTENING") in transitions
        assert core.state == S.LISTENING
        # 域事件证据：AGENT_INTERRUPTED 通知 + PLAYBACK_STOPPED 确认
        types = [e.type for e in core.bus.history]
        assert E.AGENT_INTERRUPTED in types
        assert E.PLAYBACK_STOPPED in types
        record = core.interruption.interruptions[-1]
        assert record["played_s"] == 1.3
        assert record["utterance_id"] == utterance.id
        assert record["errors"] == []

    def test_interrupt_during_thinking_cancels_in_flight(self):
        """弱打断：THINKING（LLM 在途、未出声）用户开口——停播/取消照做，
        但不算打断：utterance 静默丢弃、无 AGENT_INTERRUPTED、
        不产 interruption context（派蒙不知道自己"说过"什么）。"""
        core, playback, tts, llm = make_core()
        drive_to_thinking(core)
        utterance = core.context.begin_utterance()
        utterance.add_generated("我觉得你今天")  # 还在流式生成，未交付 TTS
        core.bus.clear_history()

        core.publish(E.USER_SPEECH_STARTED)

        assert llm.cancelled is True
        assert tts.cancel_calls == 1
        assert playback.stop_calls == 1  # 停播无害（played_s=0）
        # 弱打断语义：丢弃而非打断封账
        assert utterance.open is False
        assert utterance.discarded is True
        assert utterance.interrupted is False
        assert core.context.pending_interruption is None
        # 无 AGENT_INTERRUPTED 通知；PLAYBACK_STOPPED 照常收回状态机
        types = [e.type for e in core.bus.history]
        assert E.AGENT_INTERRUPTED not in types
        assert E.PLAYBACK_STOPPED in types
        # 生成的未出声文本不进任何历史
        all_text = "".join(
            e["text"] for e in core.context.heard_history
        ) + "".join(e["text"] for e in core.context.logical_history)
        assert "我觉得你今天" not in all_text
        assert core.interruption.interruptions[-1]["weak"] is True
        assert core.state == S.LISTENING

    def test_strong_interrupt_requires_audible_audio(self):
        """强弱分界：utterance 有已交付音频（audio_s>0）才算真打断。"""
        core, playback, tts, llm = make_core(played_s=1.3)
        drive_to_speaking(core)
        utterance = doc_utterance(core)  # audio_s = 2.0，有声
        core.bus.clear_history()

        core.publish(E.USER_SPEECH_STARTED)

        types = [e.type for e in core.bus.history]
        assert E.AGENT_INTERRUPTED in types  # 强打断：通知
        assert utterance.interrupted is True
        assert utterance.discarded is False
        assert core.interruption.interruptions[-1]["weak"] is False
        assert core.context.pending_interruption is not None  # 告诉派蒙

    def test_silence_request_interrupts_utterance(self):
        """SPEAKING 中用户要求安静：六步照跑、记录打断，状态停在 SILENCED。"""
        core, playback, tts, llm = make_core(played_s=1.3)
        drive_to_speaking(core)
        doc_utterance(core)

        core.publish(E.SILENCE_REQUESTED, {"duration_s": 60.0})

        assert playback.stop_calls == 1
        assert tts.cancel_calls == 1
        assert llm.cancelled is True
        assert core.context.pending_interruption is not None
        assert core.state == S.SILENCED  # PLAYBACK_STOPPED 被幂等忽略

    def test_repeated_speech_after_interrupt_is_idempotent(self):
        """打断后用户继续说：不重复停播/取消（utterance 已封盘）。"""
        core, playback, tts, llm = make_core(played_s=1.3)
        drive_to_speaking(core)
        doc_utterance(core)
        core.publish(E.USER_SPEECH_STARTED)
        assert playback.stop_calls == 1

        core.publish(E.USER_SPEECH_STARTED)  # 停顿后续说（非新打断）
        assert playback.stop_calls == 1
        assert tts.cancel_calls == 1
        assert core.state == S.LISTENING

    def test_interruption_fires_once_per_speech_burst(self):
        """一次打断只出现一次：VAD 在同一段用户发言里多次
        speech_started，只产生一条打断记录、一次 AGENT_INTERRUPTED。"""
        core, playback, tts, llm = make_core(played_s=1.3)
        drive_to_speaking(core)
        doc_utterance(core)
        core.bus.clear_history()

        for _ in range(3):  # 用户发言中 VAD 抖动连发
            core.publish(E.USER_SPEECH_STARTED)

        assert len(core.interruption.interruptions) == 1
        assert (
            sum(1 for e in core.bus.history if e.type == E.AGENT_INTERRUPTED)
            == 1
        )
        assert core.state == S.LISTENING

    def test_barge_in_without_utterance_recovers_to_listening(self):
        """兜底：SPEAKING 中无在途 utterance 时被打断，仍发
        PLAYBACK_STOPPED 把机器带回 LISTENING（不停留在 INTERRUPTED）。"""
        core, playback, tts, llm = make_core()
        drive_to_speaking(core)  # 没有 begin_utterance

        core.publish(E.USER_SPEECH_STARTED)

        assert playback.stop_calls == 1
        assert core.context.pending_interruption is None  # 没有可记的 utterance
        assert core.state == S.LISTENING


class TestHeardHistoryContext:
    def test_next_turn_input_matches_doc03_section3(self):
        """doc 03 §3/§5：下一轮 LLM 输入含 assistant_heard /
        assistant_generated_but_not_heard / assistant_was_interrupted 三段。"""
        core, *_ = make_core(played_s=1.3)
        drive_to_speaking(core)
        doc_utterance(core)
        core.publish(E.USER_SPEECH_STARTED)
        core.context.record_user_turn("你敢说完试试。")

        payload = core.context.build_agent_input(
            state=core.state, last_user_text="你敢说完试试。"
        )

        # §5 顶层结构
        assert payload["character"] == "paimon"
        assert payload["state"] == "LISTENING"
        assert payload["last_user_text"] == "你敢说完试试。"
        assert isinstance(payload["recent_heard_history"], list)
        assert isinstance(payload["silence_duration_ms"], int)
        assert payload["initiative_reason"] is None
        assert payload["behavior_constraints"] == {}
        # §3 interruption_context：与文档例子逐字段一致
        assert payload["interruption_context"] == {
            "assistant_heard": "我觉得你今天这个发型特别像——",
            "assistant_generated_but_not_heard": "一只刚睡醒的史莱姆。",
            "user": "你敢说完试试。",
            "event": ASSISTANT_INTERRUPTED_EVENT,
        }
        assert payload["interruption_context"]["event"] == "assistant_was_interrupted"

        # 一次性消费：再下一轮不再携带 interruption_context
        follow_up = core.context.build_agent_input(state=core.state)
        assert follow_up["interruption_context"] is None

    def test_heard_vs_logical_history_separated(self):
        """C5：logical 记完整 generated，heard 只记播出部分，互不污染。"""
        core, *_ = make_core(played_s=1.3)
        drive_to_speaking(core)
        doc_utterance(core)
        core.publish(E.USER_SPEECH_STARTED)
        core.context.record_user_turn("你敢说完试试。")

        logical = core.context.logical_history
        heard = core.context.heard_history
        # assistant 条目：logical 是全句，heard 只到已播位置
        assert logical[-2] == {
            "role": "assistant",
            "text": GENERATED,
            "heard_text": SEG_HEARD,
            "interrupted": True,
            "utterance_id": 1,
        }
        assert heard[-2] == {
            "role": "assistant",
            "text": SEG_HEARD,
            "interrupted": True,
            "utterance_id": 1,
        }
        # heard history 里不存在未播出的生成内容
        assert SEG_UNHEARD not in "".join(e["text"] for e in heard)
        # 用户轮次两边一致
        assert logical[-1]["text"] == heard[-1]["text"] == "你敢说完试试。"
        assert logical[-1]["role"] == heard[-1]["role"] == "user"

    def test_natural_completion_seals_without_interruption(self):
        """自然播完：heard=全部已交付文本，无 interruption_context。"""
        core, playback, *_ = make_core(played_s=2.0)
        drive_to_speaking(core)
        utterance = doc_utterance(core)

        core.publish(E.PLAYBACK_STOPPED, {"played_s": playback.played_s})

        assert utterance.open is False
        assert utterance.interrupted is False
        assert utterance.heard == GENERATED
        assert utterance.not_heard == ""
        assert core.context.pending_interruption is None
        assert core.state == S.IDLE  # 播完回 IDLE（doc 02 §3）
        payload = core.context.build_agent_input(state=core.state)
        assert payload["interruption_context"] is None


class TestMinBargeInHook:
    def test_short_speech_below_min_is_skipped(self):
        """C4 hook：speech_duration_s 低于 min_barge_in_s 时跳过打断
        （记入 skipped，不停播/不取消）——backchannel 容忍接线点。"""
        core, playback, tts, llm = make_core(played_s=1.3, min_barge_in_s=0.3)
        drive_to_speaking(core)
        doc_utterance(core)

        core.publish(E.USER_SPEECH_STARTED, {"speech_duration_s": 0.1})  # "嗯"

        assert len(core.interruption.skipped) == 1
        assert playback.stop_calls == 0
        assert tts.cancel_calls == 0
        assert llm.cancelled is False
        assert core.context.current_utterance.open  # utterance 未封盘

    def test_should_interrupt_predicate(self):
        """hook 语义：未带时长立即打断；带时长按阈值判定。"""
        core, *_ = make_core(min_barge_in_s=0.25)
        im = core.interruption
        from conversation import Event

        assert im.should_interrupt(Event(type=E.USER_SPEECH_STARTED)) is True
        assert (
            im.should_interrupt(
                Event(
                    type=E.USER_SPEECH_STARTED,
                    payload={"speech_duration_s": 0.3},
                )
            )
            is True
        )
        assert (
            im.should_interrupt(
                Event(
                    type=E.USER_SPEECH_STARTED,
                    payload={"speech_duration_s": 0.1},
                )
            )
            is False
        )
