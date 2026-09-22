"""Conversation 域事件与事件总线。

事件类型覆盖 `03_CONVERSATION_CORE.md` §4 全部条目，另含 §2.1 TurnManager
的四个输出事件。doc §4 明确"可以继续加……而不用推翻核心架构"，因此
Core 内部扩展事件（TURN_INCOMPLETE / STATE_CHANGED）集中在文件尾部标注。

纯逻辑层：无音频/网络依赖。bus 为同步派发，嵌套 publish 安全；
需要协程的消费者用 `subscribe_async`（要求调用时有 running loop，
否则回退 asyncio.run，便于纯同步测试驱动）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    # --- doc 03 §4 Conversation Events（规范全集，勿删） ---
    MIC_AUDIO = "MIC_AUDIO"
    USER_SPEECH_STARTED = "USER_SPEECH_STARTED"
    USER_SPEECH_STOPPED = "USER_SPEECH_STOPPED"
    TURN_COMPLETE = "TURN_COMPLETE"
    ASR_PARTIAL = "ASR_PARTIAL"
    ASR_FINAL = "ASR_FINAL"
    LLM_STARTED = "LLM_STARTED"
    LLM_TOKEN = "LLM_TOKEN"
    TTS_STARTED = "TTS_STARTED"
    FIRST_AUDIO = "FIRST_AUDIO"
    AGENT_SPEAKING = "AGENT_SPEAKING"
    AGENT_INTERRUPTED = "AGENT_INTERRUPTED"
    PLAYBACK_STOPPED = "PLAYBACK_STOPPED"
    INITIATIVE_TRIGGERED = "INITIATIVE_TRIGGERED"
    SILENCE_REQUESTED = "SILENCE_REQUESTED"
    # --- doc 03 §2.1 TurnManager 输出 ---
    USER_TURN_STARTED = "USER_TURN_STARTED"
    USER_TURN_CONTINUES = "USER_TURN_CONTINUES"
    USER_TURN_COMPLETE = "USER_TURN_COMPLETE"
    AGENT_CAN_RESPOND = "AGENT_CAN_RESPOND"
    # --- Core 扩展（doc §4 预留的演进空间） ---
    TURN_INCOMPLETE = "TURN_INCOMPLETE"  # Smart Turn 判 incomplete 的域事件
    STATE_CHANGED = "STATE_CHANGED"  # 状态机迁移通知（Terminal UI / metrics 用）
    PROMPT_PREBUILT = "PROMPT_PREBUILT"  # 投机执行：partial 驱动的 prompt 预构造完成
    AGENT_REPLY = "AGENT_REPLY"  # 结构化回复解析完成（speech/emotion/energy）
    PIPELINE_ERROR = "PIPELINE_ERROR"  # 管线环节异常（stage/error 字段；Terminal UI 展示）


#: doc 03 §4 列出的规范事件全集（验收点：一个都不能少）。
DOC03_EVENT_TYPES = frozenset(
    {
        EventType.MIC_AUDIO,
        EventType.USER_SPEECH_STARTED,
        EventType.USER_SPEECH_STOPPED,
        EventType.TURN_COMPLETE,
        EventType.ASR_PARTIAL,
        EventType.ASR_FINAL,
        EventType.LLM_STARTED,
        EventType.LLM_TOKEN,
        EventType.TTS_STARTED,
        EventType.FIRST_AUDIO,
        EventType.AGENT_SPEAKING,
        EventType.AGENT_INTERRUPTED,
        EventType.PLAYBACK_STOPPED,
        EventType.INITIATIVE_TRIGGERED,
        EventType.SILENCE_REQUESTED,
    }
)

#: TurnManager 的输出事件（doc 03 §2.1）。
TURN_MANAGER_OUTPUTS = frozenset(
    {
        EventType.USER_TURN_STARTED,
        EventType.USER_TURN_CONTINUES,
        EventType.USER_TURN_COMPLETE,
        EventType.AGENT_CAN_RESPOND,
    }
)

#: 文本注入轮次的 TURN_COMPLETE 来源标记（latency log 的 turn_source
#: 可见）；文字输入不可能是声学回声，pipeline 回声守卫据此豁免。
TEXT_TURN_SOURCE = "text_input"


@dataclass
class Event:
    """一条域事件。`seq`/`ts` 由 EventBus 在 publish 时盖章。"""

    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    seq: int = -1
    ts: float = 0.0


SyncHandler = Callable[[Event], None]
AsyncHandler = Callable[[Event], Awaitable[None]]

#: 订阅所有事件的通配键。
ALL_EVENTS = "*"


class EventBus:
    """进程内同步 pub/sub。

    - `publish` 同步按订阅顺序派发；handler 内嵌套 publish 安全（递归派发）。
    - `history` 记录全部已发布事件，供 metrics/回放/测试断言用。
    - 订阅列表在派发前快照，handler 中 subscribe/unsubscribe 不影响本轮。
    """

    def __init__(self, *, now_fn: Callable[[], float] = time.monotonic) -> None:
        self._subs: dict[EventType | str, list[SyncHandler]] = {}
        self._now = now_fn
        self._seq = 0
        self.history: list[Event] = []

    def subscribe(
        self, event_type: EventType | str, handler: SyncHandler
    ) -> SyncHandler:
        """订阅单个事件类型；`ALL_EVENTS`（"*"）订阅全部。返回 handler 便于注销。"""
        self._subs.setdefault(event_type, []).append(handler)
        return handler

    def subscribe_async(
        self, event_type: EventType | str, handler: AsyncHandler
    ) -> SyncHandler:
        """订阅协程 handler：有 running loop 时 create_task，否则 asyncio.run。"""

        def wrapper(event: Event) -> None:
            coro = handler(event)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(coro)
            else:
                loop.create_task(coro)

        return self.subscribe(event_type, wrapper)

    def unsubscribe(
        self, event_type: EventType | str, handler: SyncHandler
    ) -> None:
        handlers = self._subs.get(event_type)
        if handlers and handler in handlers:
            handlers.remove(handler)

    def publish(
        self,
        event: EventType | Event,
        payload: dict[str, Any] | None = None,
    ) -> Event:
        """发布事件：盖 seq/ts 章、入 history、同步派发给类型订阅与通配订阅。"""
        if not isinstance(event, Event):
            event = Event(type=EventType(event), payload=payload or {})
        self._seq += 1
        event.seq = self._seq
        event.ts = self._now()
        self.history.append(event)
        handlers = list(self._subs.get(event.type, ()))
        handlers += list(self._subs.get(ALL_EVENTS, ()))
        for handler in handlers:
            handler(event)
        return event

    def clear_history(self) -> None:
        self.history.clear()
