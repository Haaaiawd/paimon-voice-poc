"""对话状态机：`02_SYSTEM_ARCHITECTURE.md` §3 的最小状态集与典型路径。

状态：IDLE / LISTENING / POSSIBLE_END / THINKING / SPEAKING / INTERRUPTED /
SILENCED。典型路径：

    IDLE → LISTENING → POSSIBLE_END →(Smart Turn complete) THINKING
        → SPEAKING →(用户开口) INTERRUPTED → LISTENING

权责（防双路径，见 .loom/design/conversation-core.md）：
- VAD 迁移事件驱动听/停/打断边；轮次完成只认 TurnManager 裁决后的
  USER_TURN_COMPLETE —— VAD 停顿永远不能完成轮次（turn-taking C1）。
- SILENCE_REQUESTED 是全局迁移（除 SILENCED 自身外任意态 → SILENCED）；
  退出 = 可配置窗口超时（tick）或 wake()（唤醒词分类器接线在 TASK-011）。
- SILENCED 中用户说话不解除静默（doc 03：ASR 继续转写但不进 LLM）。

每次迁移向 bus 发 STATE_CHANGED；未知/非法触发忽略并记入 `ignored`。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum

from .events import Event, EventBus, EventType


class ConversationState(StrEnum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    POSSIBLE_END = "POSSIBLE_END"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"
    INTERRUPTED = "INTERRUPTED"
    SILENCED = "SILENCED"


#: (当前状态, 触发事件) → 下一状态。SILENCE_REQUESTED 不在表内，是全局迁移。
_TRANSITIONS: dict[
    tuple[ConversationState, EventType], ConversationState
] = {
    # IDLE：用户开口 → 听；agent 主动开口（InitiativePolicy 路径）→ 说；
    # asr_implied 轮次（VAD 漏收 start）在 IDLE 完成 → 思考
    (ConversationState.IDLE, EventType.USER_SPEECH_STARTED): ConversationState.LISTENING,
    (ConversationState.IDLE, EventType.AGENT_SPEAKING): ConversationState.SPEAKING,
    (ConversationState.IDLE, EventType.USER_TURN_COMPLETE): ConversationState.THINKING,
    # LISTENING：VAD 报停顿 → 疑似说完（不是完成，turn-taking C1）
    (ConversationState.LISTENING, EventType.USER_SPEECH_STOPPED): ConversationState.POSSIBLE_END,
    # 兜底：complete 先于/无 VAD stop 边到达（如 fallback 直判）也能进思考
    (ConversationState.LISTENING, EventType.USER_TURN_COMPLETE): ConversationState.THINKING,
    # POSSIBLE_END：用户续说 → 回听；Smart Turn 判完 → 思考
    (ConversationState.POSSIBLE_END, EventType.USER_SPEECH_STARTED): ConversationState.LISTENING,
    (ConversationState.POSSIBLE_END, EventType.USER_TURN_COMPLETE): ConversationState.THINKING,
    # THINKING：agent 出声 → 说；用户又开口 → 回听（在途响应由下游取消）；
    # 回复为空（NOOP）或失败时 pipeline 发 PLAYBACK_STOPPED 收回到 IDLE
    (ConversationState.THINKING, EventType.AGENT_SPEAKING): ConversationState.SPEAKING,
    (ConversationState.THINKING, EventType.USER_SPEECH_STARTED): ConversationState.LISTENING,
    (ConversationState.THINKING, EventType.PLAYBACK_STOPPED): ConversationState.IDLE,
    # SPEAKING：barge-in（doc 03 §2.2）；自然播完 → IDLE
    (ConversationState.SPEAKING, EventType.USER_SPEECH_STARTED): ConversationState.INTERRUPTED,
    (ConversationState.SPEAKING, EventType.AGENT_INTERRUPTED): ConversationState.INTERRUPTED,
    (ConversationState.SPEAKING, EventType.PLAYBACK_STOPPED): ConversationState.IDLE,
    # INTERRUPTED：停播确认 → 重新听用户说（doc 03 §2.2 第 6 步）
    (ConversationState.INTERRUPTED, EventType.PLAYBACK_STOPPED): ConversationState.LISTENING,
}

#: 状态机消费的事件类型（订阅 bus 用；SILENCE_REQUESTED 走全局分支）。
_TRIGGER_EVENTS = frozenset(
    {et for _, et in _TRANSITIONS} | {EventType.SILENCE_REQUESTED}
)

DEFAULT_SILENCE_WINDOW_S = 60.0


class ConversationStateMachine:
    """事件驱动的对话状态机。

    - 传 `bus` 则自动订阅 `_TRIGGER_EVENTS`；也可脱离 bus 直接
      `dispatch(Event)` 驱动（纯单测友好）。
    - `silence_window_s` 为 SILENCED 默认窗口；SILENCE_REQUESTED payload
      可带 `duration_s` 覆盖（如用户说"两分钟"）。
    - `ignored` 记录被忽略的触发（非法迁移/状态下不适用的事件）。
    """

    def __init__(
        self,
        bus: EventBus | None = None,
        *,
        silence_window_s: float = DEFAULT_SILENCE_WINDOW_S,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._state = ConversationState.IDLE
        self._silence_window_s = silence_window_s
        self._now = now_fn
        self._silenced_until: float | None = None
        self.ignored: list[Event] = []
        if bus is not None:
            for et in _TRIGGER_EVENTS:
                bus.subscribe(et, self.dispatch)
            # SILENCE_REQUESTED 已在 _TRIGGER_EVENTS；无需重复订阅。

    @property
    def state(self) -> ConversationState:
        return self._state

    @property
    def silenced_remaining_s(self) -> float | None:
        if self._state != ConversationState.SILENCED or self._silenced_until is None:
            return None
        return max(0.0, self._silenced_until - self._now())

    def dispatch(self, event: Event) -> bool:
        """按事件尝试迁移。返回是否发生了状态变化。"""
        if event.type == EventType.SILENCE_REQUESTED:
            if self._state == ConversationState.SILENCED:
                self.ignored.append(event)
                return False
            duration = event.payload.get("duration_s", self._silence_window_s)
            return self._apply(
                ConversationState.SILENCED, event.type, silenced_for=duration
            )
        target = _TRANSITIONS.get((self._state, event.type))
        if target is None:
            self.ignored.append(event)
            return False
        return self._apply(target, event.type)

    def tick(self, now: float | None = None) -> bool:
        """时钟推进：SILENCED 窗口到期自动回 IDLE。返回是否发生迁移。"""
        if (
            self._state == ConversationState.SILENCED
            and self._silenced_until is not None
            and (now if now is not None else self._now()) >= self._silenced_until
        ):
            return self._apply(ConversationState.IDLE, "silence_timeout")
        return False

    def wake(self) -> bool:
        """唤醒词/系统事件解除静默（唤醒词分类器在 TASK-011 接线）。"""
        if self._state != ConversationState.SILENCED:
            return False
        return self._apply(ConversationState.IDLE, "wake")

    def _apply(
        self,
        target: ConversationState,
        trigger: EventType | str,
        *,
        silenced_for: float | None = None,
    ) -> bool:
        previous = self._state
        self._state = target
        if target == ConversationState.SILENCED:
            self._silenced_until = self._now() + (silenced_for or self._silence_window_s)
        elif previous == ConversationState.SILENCED:
            self._silenced_until = None
        if self._bus is not None:
            self._bus.publish(
                EventType.STATE_CHANGED,
                {
                    "from": str(previous),
                    "to": str(target),
                    "trigger": str(trigger),
                },
            )
        return True
