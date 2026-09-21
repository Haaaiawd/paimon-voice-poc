"""ConversationCore：事件总线 + 状态机 + 域组件的组装点。

纯逻辑层 facade：pipeline 组装（TASK-010）与单测共用一个接线入口。
订阅顺序即派发顺序：machine → turn_manager → context → interruption，
保证 USER_SPEECH_STARTED 到达 InterruptionManager 时状态迁移已完成。
InitiativePolicy 在 TASK-011 加入本组装。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .context import ContextManager
from .events import Event, EventBus, EventType
from .interruption import InterruptionManager, PlaybackLike, TTSLike
from .state_machine import (
    DEFAULT_SILENCE_WINDOW_S,
    ConversationState,
    ConversationStateMachine,
)
from .turn_manager import TurnManager


class ConversationCore:
    """一根 bus 上挂状态机、轮次裁决器、双历史与打断执行器。"""

    def __init__(
        self,
        *,
        bus: EventBus | None = None,
        silence_window_s: float = DEFAULT_SILENCE_WINDOW_S,
        playback: PlaybackLike | None = None,
        tts: TTSLike | None = None,
        min_barge_in_s: float = 0.0,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bus = bus or EventBus(now_fn=now_fn)
        self.machine = ConversationStateMachine(
            self.bus, silence_window_s=silence_window_s, now_fn=now_fn
        )
        self.turn_manager = TurnManager(self.bus, self.machine)
        self.context = ContextManager(self.bus)
        self.interruption = InterruptionManager(
            self.bus,
            self.machine,
            self.context,
            playback=playback,
            tts=tts,
            min_barge_in_s=min_barge_in_s,
            now_fn=now_fn,
        )

    @property
    def state(self) -> ConversationState:
        return self.machine.state

    def publish(
        self, event_type: EventType, payload: dict[str, Any] | None = None
    ) -> Event:
        return self.bus.publish(event_type, payload)

    def tick(self, now: float | None = None) -> bool:
        return self.machine.tick(now)

    def wake(self) -> bool:
        return self.machine.wake()
