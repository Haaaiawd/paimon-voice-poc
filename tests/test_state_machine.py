"""TASK-005 acceptance 1：事件总线覆盖 doc 03 §4 全部事件类型；
状态机迁移覆盖 doc 02 §3 典型路径（含 SILENCED 窗口与 barge-in）。

verify_by: pytest tests/test_state_machine.py 覆盖全部状态迁移。
"""

from __future__ import annotations

import pytest

from conversation import (
    ALL_EVENTS,
    DOC03_EVENT_TYPES,
    ConversationCore,
    ConversationState,
    ConversationStateMachine,
    Event,
    EventBus,
    EventType,
)

S = ConversationState
E = EventType

# (from_state, trigger_event, to_state)：状态机迁移表全行 + 全局 SILENCE_REQUESTED。
TABLE_ROWS = [
    (S.IDLE, E.USER_SPEECH_STARTED, S.LISTENING),
    (S.IDLE, E.AGENT_SPEAKING, S.SPEAKING),  # 主动开口（InitiativePolicy 路径）
    (S.LISTENING, E.USER_SPEECH_STOPPED, S.POSSIBLE_END),
    (S.LISTENING, E.USER_TURN_COMPLETE, S.THINKING),  # 无 VAD stop 边的兜底
    (S.POSSIBLE_END, E.USER_SPEECH_STARTED, S.LISTENING),
    (S.POSSIBLE_END, E.USER_TURN_COMPLETE, S.THINKING),
    (S.THINKING, E.AGENT_SPEAKING, S.SPEAKING),
    (S.THINKING, E.USER_SPEECH_STARTED, S.LISTENING),
    (S.SPEAKING, E.USER_SPEECH_STARTED, S.INTERRUPTED),  # barge-in
    (S.SPEAKING, E.AGENT_INTERRUPTED, S.INTERRUPTED),
    (S.SPEAKING, E.PLAYBACK_STOPPED, S.IDLE),  # 自然播完
    (S.INTERRUPTED, E.PLAYBACK_STOPPED, S.LISTENING),
]

#: 把状态机驱动到指定状态的最短事件序列（不经过 SILENCED）。
PATHS: dict[S, list[E]] = {
    S.IDLE: [],
    S.LISTENING: [E.USER_SPEECH_STARTED],
    S.POSSIBLE_END: [E.USER_SPEECH_STARTED, E.USER_SPEECH_STOPPED],
    S.THINKING: [
        E.USER_SPEECH_STARTED,
        E.USER_SPEECH_STOPPED,
        E.USER_TURN_COMPLETE,
    ],
    S.SPEAKING: [
        E.USER_SPEECH_STARTED,
        E.USER_SPEECH_STOPPED,
        E.USER_TURN_COMPLETE,
        E.AGENT_SPEAKING,
    ],
    S.INTERRUPTED: [
        E.USER_SPEECH_STARTED,
        E.USER_SPEECH_STOPPED,
        E.USER_TURN_COMPLETE,
        E.AGENT_SPEAKING,
        E.USER_SPEECH_STARTED,
    ],
}


def drive(machine: ConversationStateMachine, events: list[E]) -> None:
    for et in events:
        machine.dispatch(Event(type=et))


def force_state(machine: ConversationStateMachine, target: S) -> None:
    drive(machine, PATHS[target])
    assert machine.state == target


class TestEventBusCoverage:
    def test_doc03_all_event_types_defined(self):
        """事件总线覆盖 doc 03 §4 全部 15 个事件类型。"""
        assert len(DOC03_EVENT_TYPES) == 15
        for et in DOC03_EVENT_TYPES:
            assert et in EventType
            assert et.value == et.name

    def test_turn_manager_outputs_defined(self):
        """doc 03 §2.1 的四个轮次输出事件均在 EventType 中。"""
        for name in (
            "USER_TURN_STARTED",
            "USER_TURN_CONTINUES",
            "USER_TURN_COMPLETE",
            "AGENT_CAN_RESPOND",
        ):
            assert EventType[name].value == name

    def test_publish_dispatch_and_history(self):
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe(E.ASR_PARTIAL, seen.append)
        bus.subscribe(ALL_EVENTS, seen.append)
        ev = bus.publish(E.ASR_PARTIAL, {"text": "嗯"})
        assert len(seen) == 2 and seen[0] is ev and seen[1] is ev
        assert bus.history == [ev]
        assert ev.seq == 1 and ev.ts >= 0
        ev2 = bus.publish(E.LLM_TOKEN, {"text": "哈"})
        assert ev2.seq == 2 and bus.history == [ev, ev2]

    def test_nested_publish_and_unsubscribe(self):
        bus = EventBus()
        order: list[str] = []

        def republish(_e: Event) -> None:
            order.append("outer")
            bus.publish(E.MIC_AUDIO)

        bus.subscribe(E.USER_SPEECH_STARTED, republish)
        bus.subscribe(E.MIC_AUDIO, lambda e: order.append("nested"))
        bus.subscribe(ALL_EVENTS, lambda e: order.append(e.type.value))
        bus.publish(E.USER_SPEECH_STARTED)
        # 嵌套 publish 同步递归派发：外层 handler → 内层类型+通配 → 外层通配
        assert order == ["outer", "nested", "MIC_AUDIO", "USER_SPEECH_STARTED"]

        calls: list[Event] = []
        h = bus.subscribe(E.TTS_STARTED, calls.append)
        bus.unsubscribe(E.TTS_STARTED, h)
        bus.publish(E.TTS_STARTED)
        assert calls == []


class TestTypicalPath:
    def test_doc02_section3_typical_path(self):
        """doc 02 §3：IDLE→LISTENING→POSSIBLE_END→THINKING→SPEAKING→
        INTERRUPTED→LISTENING 一条典型路径走通。"""
        bus = EventBus()
        machine = ConversationStateMachine(bus)
        seq = [
            E.USER_SPEECH_STARTED,  # 用户开口
            E.USER_SPEECH_STOPPED,  # VAD 停顿（不是完成）
            E.USER_TURN_COMPLETE,  # Smart Turn 判 complete
            E.AGENT_SPEAKING,  # 派蒙出声
            E.USER_SPEECH_STARTED,  # 用户插嘴
            E.PLAYBACK_STOPPED,  # 停播确认 → 重新听
        ]
        for et in seq:
            bus.publish(et)
        changes = [
            e.payload for e in bus.history if e.type == E.STATE_CHANGED
        ]
        path = [(c["from"], c["to"]) for c in changes]
        assert path == [
            ("IDLE", "LISTENING"),
            ("LISTENING", "POSSIBLE_END"),
            ("POSSIBLE_END", "THINKING"),
            ("THINKING", "SPEAKING"),
            ("SPEAKING", "INTERRUPTED"),
            ("INTERRUPTED", "LISTENING"),
        ]
        assert machine.state == S.LISTENING


class TestTransitionTable:
    @pytest.mark.parametrize("start,trigger,target", TABLE_ROWS)
    def test_each_table_row(self, start, trigger, target):
        machine = ConversationStateMachine()
        force_state(machine, start)
        assert machine.dispatch(Event(type=trigger)) is True
        assert machine.state == target

    @pytest.mark.parametrize("start", list(S))
    def test_silence_requested_is_global(self, start):
        """SILENCE_REQUESTED 从任意非 SILENCED 态进入 SILENCED。"""
        if start == S.SILENCED:
            pytest.skip("SILENCED 自身无此迁移")
        machine = ConversationStateMachine()
        force_state(machine, start)
        assert machine.dispatch(Event(type=E.SILENCE_REQUESTED)) is True
        assert machine.state == S.SILENCED

    @pytest.mark.parametrize(
        "start,trigger",
        [
            (S.IDLE, E.USER_SPEECH_STOPPED),
            (S.IDLE, E.USER_TURN_COMPLETE),
            (S.IDLE, E.PLAYBACK_STOPPED),
            (S.LISTENING, E.AGENT_SPEAKING),  # 硬规则：用户说话时不开口
            (S.POSSIBLE_END, E.AGENT_SPEAKING),  # 停顿≠完成，agent 不抢话
            (S.INTERRUPTED, E.USER_SPEECH_STOPPED),
            (S.SPEAKING, E.USER_TURN_COMPLETE),
        ],
    )
    def test_illegal_triggers_ignored(self, start, trigger):
        machine = ConversationStateMachine()
        force_state(machine, start)
        assert machine.dispatch(Event(type=trigger)) is False
        assert machine.state == start
        assert machine.ignored[-1].type == trigger

    def test_unconsumed_event_types_ignored(self):
        """总线上有但状态机不消费的事件（ASR/LLM/TTS 数据面）不改变状态。"""
        machine = ConversationStateMachine()
        for et in (
            E.MIC_AUDIO,
            E.ASR_PARTIAL,
            E.ASR_FINAL,
            E.TURN_COMPLETE,  # 原始裁决不直接驱动状态机
            E.TURN_INCOMPLETE,
            E.LLM_TOKEN,
            E.TTS_STARTED,
            E.FIRST_AUDIO,
            E.INITIATIVE_TRIGGERED,
        ):
            machine.dispatch(Event(type=et))
        assert machine.state == S.IDLE
        assert len(machine.ignored) == 9


class TestSilenced:
    def test_timeout_exits_to_idle(self):
        self_now = [1000.0]
        machine = ConversationStateMachine(now_fn=lambda: self_now[0])
        machine.dispatch(Event(type=E.SILENCE_REQUESTED))
        assert machine.state == S.SILENCED
        self_now[0] += 30.0
        assert machine.tick() is False  # 默认 60s 窗口未到
        assert machine.state == S.SILENCED
        self_now[0] += 30.0
        assert machine.tick() is True
        assert machine.state == S.IDLE

    def test_duration_override_in_payload(self):
        """SILENCE_REQUESTED 可带 duration_s 覆盖默认窗口（如"两分钟"）。"""
        self_now = [0.0]
        machine = ConversationStateMachine(now_fn=lambda: self_now[0])
        machine.dispatch(Event(type=E.SILENCE_REQUESTED, payload={"duration_s": 120.0}))
        self_now[0] = 61.0
        assert machine.tick() is False
        self_now[0] = 120.0
        assert machine.tick() is True
        assert machine.state == S.IDLE

    def test_wake_exits_silenced(self):
        machine = ConversationStateMachine()
        machine.dispatch(Event(type=E.SILENCE_REQUESTED))
        assert machine.wake() is True
        assert machine.state == S.IDLE
        assert machine.wake() is False  # 非 SILENCED 下 wake 无效

    def test_user_speech_does_not_exit_silenced(self):
        """doc：SILENCED 中 ASR 继续转写但用户说话本身不解静默。"""
        machine = ConversationStateMachine()
        machine.dispatch(Event(type=E.SILENCE_REQUESTED))
        machine.dispatch(Event(type=E.USER_SPEECH_STARTED))
        machine.dispatch(Event(type=E.USER_SPEECH_STOPPED))
        assert machine.state == S.SILENCED
        assert machine.silenced_remaining_s is not None

    def test_silence_requested_in_silenced_ignored(self):
        machine = ConversationStateMachine()
        machine.dispatch(Event(type=E.SILENCE_REQUESTED))
        assert machine.dispatch(Event(type=E.SILENCE_REQUESTED)) is False
        assert machine.state == S.SILENCED


class TestIntegrationWithCore:
    def test_state_changed_events_on_bus(self):
        core = ConversationCore()
        states: list[str] = []
        core.bus.subscribe(
            E.STATE_CHANGED, lambda e: states.append(e.payload["to"])
        )
        core.publish(E.USER_SPEECH_STARTED)
        core.publish(E.USER_SPEECH_STOPPED)
        assert states == ["LISTENING", "POSSIBLE_END"]
        assert core.state == S.POSSIBLE_END

    def test_vad_pause_never_thinking(self):
        """turn-taking C1：只发 VAD 停顿永远到不了 THINKING。"""
        core = ConversationCore()
        core.publish(E.USER_SPEECH_STARTED)
        core.publish(E.USER_SPEECH_STOPPED)
        assert core.state == S.POSSIBLE_END
        assert core.state != S.THINKING
