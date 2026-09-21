"""ConversationCore：事件总线 + 状态机 + TurnManager 的组装点。

纯逻辑层 facade：pipeline 组装（TASK-010）与单测共用一个接线入口。
InterruptionManager / InitiativePolicy / ContextManager 在各自 Task 中
加入本组装（TASK-006 / TASK-011），不在本层展开。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .events import Event, EventBus, EventType
from .state_machine import (
    DEFAULT_SILENCE_WINDOW_S,
    ConversationState,
    ConversationStateMachine,
)
from .turn_manager import TurnManager


class ConversationCore:
    """一根 bus 上挂状态机与轮次裁决器；对外只暴露状态与发布入口。"""

    def __init__(
        self,
        *,
        bus: EventBus | None = None,
        silence_window_s: float = DEFAULT_SILENCE_WINDOW_S,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bus = bus or EventBus(now_fn=now_fn)
        self.machine = ConversationStateMachine(
            self.bus, silence_window_s=silence_window_s, now_fn=now_fn
        )
        self.turn_manager = TurnManager(self.bus, self.machine)

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
