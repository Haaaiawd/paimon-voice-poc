"""TASK-005 acceptance 2：TurnManager 消费 VAD + SmartTurn + ASR partial
事件，输出 USER_TURN_STARTED / CONTINUES / COMPLETE / AGENT_CAN_RESPOND。

verify_by: pytest tests/test_turn_manager.py 用录制事件序列驱动断言。
"""

from __future__ import annotations

from conversation import (
    TURN_MANAGER_OUTPUTS,
    ConversationCore,
    ConversationState,
    Event,
    EventType,
    TurnManager,
)

S = ConversationState
E = EventType

#: 录制事件序列（模拟上游 adapter 产出的域事件流）。
CLEAN_TURN = [
    (E.USER_SPEECH_STARTED, {}),
    (E.ASR_PARTIAL, {"text": "我觉得"}),
    (E.ASR_PARTIAL, {"text": "我觉得这个比赛"}),
    (E.ASR_FINAL, {"text": "我觉得这个比赛吧"}),
    (E.USER_SPEECH_STOPPED, {}),
    (E.TURN_COMPLETE, {"source": "model", "probability": 0.87}),
]


def make_core() -> tuple[ConversationCore, list[Event]]:
    """组装 Core 并录制 TurnManager 的全部输出事件。"""
    core = ConversationCore()
    outputs: list[Event] = []
    for et in TURN_MANAGER_OUTPUTS:
        core.bus.subscribe(et, outputs.append)
    return core, outputs


def feed(core: ConversationCore, seq: list[tuple[E, dict]]) -> None:
    """按录制序列回放域事件。"""
    for et, payload in seq:
        core.publish(et, payload)


def kinds(events: list[Event]) -> list[E]:
    return [e.type for e in events]


class TestCleanTurn:
    def test_full_turn_sequence(self):
        """干净轮次：started→partial→final→stop→complete →
        STARTED/CONTINUES/COMPLETE/AGENT_CAN_RESPOND 全齐。"""
        core, outputs = make_core()
        feed(core, CLEAN_TURN)

        assert kinds(outputs) == [
            E.USER_TURN_STARTED,
            E.USER_TURN_CONTINUES,
            E.USER_TURN_CONTINUES,
            E.USER_TURN_CONTINUES,
            E.USER_TURN_COMPLETE,
            E.AGENT_CAN_RESPOND,
        ]
        started, *_, complete, can_respond = outputs
        assert started.payload["barge_in"] is False
        assert complete.payload["text"] == "我觉得这个比赛吧"
        assert complete.payload["source"] == "model"
        assert can_respond.payload["text"] == "我觉得这个比赛吧"
        # 同一轮次 turn_id 一致；状态机走到 THINKING
        assert len({e.payload["turn_id"] for e in outputs}) == 1
        assert core.state == S.THINKING

    def test_turn_ids_increment(self):
        core, outputs = make_core()
        feed(core, CLEAN_TURN)
        feed(core, CLEAN_TURN)
        ids = [e.payload["turn_id"] for e in outputs if e.type == E.USER_TURN_STARTED]
        assert ids == [1, 2]


class TestPauseIsNotComplete:
    def test_vad_stop_alone_never_completes_turn(self):
        """turn-taking C1：VAD 停顿事件不产生 USER_TURN_COMPLETE /
        AGENT_CAN_RESPOND —— 停顿不是完成。"""
        core, outputs = make_core()
        feed(
            core,
            [
                (E.USER_SPEECH_STARTED, {}),
                (E.ASR_PARTIAL, {"text": "我觉得这个比赛吧……"}),
                (E.USER_SPEECH_STOPPED, {}),
            ],
        )
        assert E.USER_TURN_COMPLETE not in kinds(outputs)
        assert E.AGENT_CAN_RESPOND not in kinds(outputs)
        assert core.turn_manager.turn_open
        assert core.state == S.POSSIBLE_END  # 等待 Smart Turn 裁决

    def test_pause_resume_then_complete(self):
        """句中停顿→Smart Turn 判 incomplete→续说→完成：长轮次不被切开。"""
        core, outputs = make_core()
        feed(
            core,
            [
                (E.USER_SPEECH_STARTED, {}),
                (E.ASR_PARTIAL, {"text": "我觉得这个比赛吧……"}),
                (E.USER_SPEECH_STOPPED, {}),
                (E.TURN_INCOMPLETE, {"probability": 0.2}),
                (E.USER_SPEECH_STARTED, {}),
                (E.ASR_PARTIAL, {"text": "我觉得这个比赛吧，其实还挺好看的"}),
                (E.USER_SPEECH_STOPPED, {}),
                (E.TURN_COMPLETE, {"source": "model", "probability": 0.9}),
            ],
        )
        assert kinds(outputs) == [
            E.USER_TURN_STARTED,
            E.USER_TURN_CONTINUES,  # asr_partial
            E.USER_TURN_CONTINUES,  # turn_incomplete：裁决不是完成，轮次继续
            E.USER_TURN_CONTINUES,  # speech_resumed
            E.USER_TURN_CONTINUES,  # asr_partial
            E.USER_TURN_COMPLETE,
            E.AGENT_CAN_RESPOND,
        ]
        reasons = [e.payload.get("reason") for e in outputs]
        assert "turn_incomplete" in reasons
        assert "speech_resumed" in reasons
        assert len({e.payload["turn_id"] for e in outputs}) == 1  # 没被切成两轮

    def test_silence_fallback_complete_carries_source(self):
        """C3 兜底路径：source=silence_fallback 透传到 USER_TURN_COMPLETE。"""
        core, outputs = make_core()
        feed(
            core,
            [
                (E.USER_SPEECH_STARTED, {}),
                (E.USER_SPEECH_STOPPED, {}),
                (E.TURN_COMPLETE, {"source": "silence_fallback"}),
            ],
        )
        complete = outputs[-2]
        assert complete.type == E.USER_TURN_COMPLETE
        assert complete.payload["source"] == "silence_fallback"
        assert outputs[-1].type == E.AGENT_CAN_RESPOND


class TestBargeIn:
    def test_user_speech_during_speaking_is_barge_in(self):
        """SPEAKING 中用户开口：新轮次 barge_in=True；TASK-006 接线后
        InterruptionManager 同步完成停播确认，状态 SPEAKING→INTERRUPTED→
        LISTENING 一次走完。"""
        core, outputs = make_core()
        feed(core, CLEAN_TURN)
        core.publish(E.AGENT_SPEAKING)  # THINKING → SPEAKING
        assert core.state == S.SPEAKING
        outputs.clear()

        feed(core, [(E.USER_SPEECH_STARTED, {}), (E.ASR_PARTIAL, {"text": "你敢"})])
        assert kinds(outputs)[:2] == [E.USER_TURN_STARTED, E.USER_TURN_CONTINUES]
        assert outputs[0].payload["barge_in"] is True
        transitions = [
            (e.payload["from"], e.payload["to"])
            for e in core.bus.history
            if e.type == E.STATE_CHANGED
        ]
        assert transitions[-2:] == [
            ("SPEAKING", "INTERRUPTED"),
            ("INTERRUPTED", "LISTENING"),
        ]
        assert core.state == S.LISTENING
        feed(
            core,
            [
                (E.USER_SPEECH_STOPPED, {}),
                (E.TURN_COMPLETE, {"source": "model"}),
            ],
        )
        assert kinds(outputs)[-2:] == [E.USER_TURN_COMPLETE, E.AGENT_CAN_RESPOND]
        assert outputs[-1].payload["text"] == "你敢"


class TestSilencedGating:
    def test_no_agent_can_respond_while_silenced(self):
        """SILENCED 中轮次照常裁决（发 USER_TURN_COMPLETE），但不发
        AGENT_CAN_RESPOND —— 静默中不进 LLM。"""
        core, outputs = make_core()
        core.publish(E.SILENCE_REQUESTED, {"duration_s": 120.0})
        assert core.state == S.SILENCED

        feed(
            core,
            [
                (E.USER_SPEECH_STARTED, {}),
                (E.ASR_FINAL, {"text": "派蒙你先闭嘴"}),
                (E.USER_SPEECH_STOPPED, {}),
                (E.TURN_COMPLETE, {"source": "model"}),
            ],
        )
        assert E.USER_TURN_COMPLETE in kinds(outputs)  # 轮次事实仍记录
        assert E.AGENT_CAN_RESPOND not in kinds(outputs)
        assert core.state == S.SILENCED  # 用户说话不解静默


class TestEdgeCases:
    def test_stale_turn_complete_ignored(self):
        """无开轮次时到达的 TURN_COMPLETE（陈旧兜底裁决）不产生输出。"""
        core, outputs = make_core()
        feed(core, [(E.TURN_COMPLETE, {"source": "silence_fallback"})])
        assert outputs == []

    def test_asr_implies_turn_when_vad_start_missed(self):
        """鲁棒性：漏收 VAD start 时 ASR 事件兜底开轮次。"""
        core, outputs = make_core()
        feed(
            core,
            [
                (E.ASR_PARTIAL, {"text": "嗯"}),
                (E.TURN_COMPLETE, {"source": "model"}),
            ],
        )
        assert kinds(outputs) == [
            E.USER_TURN_STARTED,
            E.USER_TURN_CONTINUES,
            E.USER_TURN_COMPLETE,
            E.AGENT_CAN_RESPOND,
        ]
        assert outputs[0].payload["reason"] == "asr_implied"

    def test_second_complete_same_turn_ignored(self):
        core, outputs = make_core()
        feed(core, CLEAN_TURN)
        n = len(outputs)
        feed(core, [(E.TURN_COMPLETE, {"source": "silence_fallback"})])
        assert len(outputs) == n  # 轮次已关，重复 complete 不重复输出

    def test_outputs_subset_of_declared(self):
        """TurnManager 只发 §2.1 声明的四个输出事件。"""
        core = ConversationCore()
        tm: TurnManager = core.turn_manager
        emitted = {
            e.type
            for e in core.bus.history
            if e.type in TURN_MANAGER_OUTPUTS
        }
        feed(core, CLEAN_TURN)
        emitted |= {
            e.type
            for e in core.bus.history
            if e.type in TURN_MANAGER_OUTPUTS
        }
        assert emitted <= TURN_MANAGER_OUTPUTS
        assert emitted == TURN_MANAGER_OUTPUTS  # 四种全部实际发出
        assert tm.turn_open is False
