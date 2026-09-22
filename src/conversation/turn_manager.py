"""TurnManager：消费 VAD + Smart Turn + ASR 事件，裁决并发出用户轮次事件。

doc 03 §2.1：输入 VAD 状态 / Smart Turn 状态 / ASR partial|final /
当前 Conversation State；输出 USER_TURN_STARTED / CONTINUES / COMPLETE /
AGENT_CAN_RESPOND。原则：**停顿不是完成** —— VAD 的 USER_SPEECH_STOPPED
只让状态机进 POSSIBLE_END，本类从不据它判轮次完成；完成只认 Smart Turn
裁决后的 TURN_COMPLETE（turn-taking C1：轮次权归模型，不裸用 VAD 超时）。

事件语义：
- USER_TURN_STARTED：新用户轮次开始（barge_in=True 表示打断了 agent）。
- USER_TURN_CONTINUES：轮次仍在继续的证据——ASR partial/final 更新、
  停顿后续说、Smart Turn 判 incomplete。
- USER_TURN_COMPLETE：轮次被裁决完成，payload 带当前 transcript 与
  complete 来源（model / silence_fallback）。
- AGENT_CAN_RESPOND：完成且未被静默时发出，LLM 层凭它行动（TASK-005
  边界：LLM 未接入，本事件只发不收）。SILENCED 中轮次照常裁决但不发
  本事件（doc：静默中不进 LLM）。
"""

from __future__ import annotations

from .events import Event, EventBus, EventType
from .state_machine import ConversationState, ConversationStateMachine

_INPUT_EVENTS = (
    EventType.USER_SPEECH_STARTED,
    EventType.USER_SPEECH_STOPPED,
    EventType.TURN_COMPLETE,
    EventType.TURN_INCOMPLETE,
    EventType.ASR_PARTIAL,
    EventType.ASR_FINAL,
)


class TurnManager:
    """用户轮次裁决器。持有状态机引用以读"当前 Conversation State"。"""

    def __init__(
        self,
        bus: EventBus,
        machine: ConversationStateMachine,
        *,
        tail_asr_window_s: float = 2.0,
    ) -> None:
        self._bus = bus
        self._machine = machine
        self._turn_open = False
        self._turn_id = 0
        self._last_text = ""
        self._awaiting_verdict = False
        #: 轮次关闭时刻（事件 ts）：窗内迟到转写视为上一轮的尾帧。
        self._turn_closed_ts: float | None = None
        self._tail_window_s = tail_asr_window_s
        for et in _INPUT_EVENTS:
            bus.subscribe(et, self._on_event)

    @property
    def turn_open(self) -> bool:
        return self._turn_open

    @property
    def turn_id(self) -> int:
        return self._turn_id

    @property
    def last_text(self) -> str:
        return self._last_text

    def _on_event(self, event: Event) -> None:
        match event.type:
            case EventType.USER_SPEECH_STARTED:
                self._on_speech_started()
            case EventType.USER_SPEECH_STOPPED:
                # 停顿不是完成：只标记等待裁决，迁移 POSSIBLE_END 归状态机。
                if self._turn_open:
                    self._awaiting_verdict = True
            case EventType.ASR_PARTIAL | EventType.ASR_FINAL:
                self._on_asr(event)
            case EventType.TURN_INCOMPLETE:
                self._on_turn_incomplete(event)
            case EventType.TURN_COMPLETE:
                self._on_turn_complete(event)

    def _on_speech_started(self) -> None:
        if self._turn_open:
            # 停顿后重新开口：轮次继续，撤销"等待裁决"。
            self._awaiting_verdict = False
            self._emit(
                EventType.USER_TURN_CONTINUES,
                {"turn_id": self._turn_id, "reason": "speech_resumed"},
            )
            return
        self._open_turn(
            {
                "barge_in": self._machine.state
                in (ConversationState.SPEAKING, ConversationState.INTERRUPTED),
                "reason": "speech_started",
            }
        )

    def _on_asr(self, event: Event) -> None:
        # ASR 出声证明有语音：漏收 VAD start 时兜底开轮次，保证可测试性/鲁棒性。
        # 但轮次关闭后（THINKING/SPEAKING/INTERRUPTED）迟到的转写是上一轮的
        # 尾帧——wait_for_transcript 闸门下 ASR final 常与 TURN_COMPLETE 同刻
        # 到达，此时再开轮次会留下永不关闭的幻影轮（下个 barge-in 会被错误
        # 地判成 speech_resumed 续轮）。
        if not self._turn_open:
            if self._machine.state in (
                ConversationState.THINKING,
                ConversationState.SPEAKING,
                ConversationState.INTERRUPTED,
            ):
                return
            if (
                self._turn_closed_ts is not None
                and event.ts - self._turn_closed_ts < self._tail_window_s
            ):
                # 快速响应可赶在 ASR 尾帧前回到 IDLE；窗内无 VAD start 的
                # 转写是已关闭轮次的尾巴。真开口自有 USER_SPEECH_STARTED 开轮。
                return
            self._open_turn({"reason": "asr_implied", "barge_in": False})
        text = event.payload.get("text", "")
        if text:
            self._last_text = text
        self._emit(
            EventType.USER_TURN_CONTINUES,
            {
                "turn_id": self._turn_id,
                "reason": "asr_final"
                if event.type == EventType.ASR_FINAL
                else "asr_partial",
                "text": self._last_text,
            },
        )

    def _on_turn_incomplete(self, event: Event) -> None:
        if not self._turn_open:
            return
        self._awaiting_verdict = False
        self._emit(
            EventType.USER_TURN_CONTINUES,
            {
                "turn_id": self._turn_id,
                "reason": "turn_incomplete",
                "probability": event.payload.get("probability"),
            },
        )

    def _on_turn_complete(self, event: Event) -> None:
        if not self._turn_open:
            return  # 陈旧裁决（如兜底超时在轮次关闭后才到）：忽略。
        self._turn_open = False
        self._awaiting_verdict = False
        self._turn_closed_ts = event.ts
        can_respond = self._machine.state != ConversationState.SILENCED
        self._emit(
            EventType.USER_TURN_COMPLETE,
            {
                "turn_id": self._turn_id,
                "text": self._last_text,
                "source": event.payload.get("source"),
                "probability": event.payload.get("probability"),
            },
        )
        if can_respond:
            self._emit(
                EventType.AGENT_CAN_RESPOND,
                {"turn_id": self._turn_id, "text": self._last_text},
            )

    def _open_turn(self, payload: dict) -> None:
        self._turn_id += 1
        self._turn_open = True
        self._last_text = ""
        self._awaiting_verdict = False
        self._emit(EventType.USER_TURN_STARTED, {"turn_id": self._turn_id, **payload})

    def _emit(self, event_type: EventType, payload: dict) -> Event:
        return self._bus.publish(event_type, payload)
