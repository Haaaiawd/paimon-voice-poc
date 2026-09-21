"""InterruptionManager：doc 03 §2.2 的六步打断执行器。

触发（turn-taking C4：媒体层立即停 + 逻辑层取消，两层同时生效）：
- USER_SPEECH_STARTED / AGENT_INTERRUPTED 且存在在途 utterance（SPEAKING 中
  用户重新开口）；SPEAKING→INTERRUPTED 的状态迁移由状态机在同一事件上完成，
  本类只管副作用，不重复裁决。
- SILENCE_REQUESTED 且有在途 utterance：用户要求安静优先级最高，同样打断
  （机器已在 SILENCED，PLAYBACK_STOPPED 被幂等忽略，状态保持静默）。
- 兜底：触发时状态已在 INTERRUPTED（如外部 AGENT_INTERRUPTED 未结清），
  重跑六步是幂等恢复——stop/cancel 无害，封盘只对 open utterance 生效，
  PLAYBACK_STOPPED 把机器带回 LISTENING。

六步（doc §2.2，顺序即语义）：
1. 停止音频播放（playback.stop() → played_s，"用户实际听到哪"的最佳近似）；
2. 清空未播放 TTS buffer（TTSProvider.cancel 契约：停推+清 buffer+可重开）；
3. 取消在途 LLM（track_llm 登记的 cancel 回调——陈旧响应是 barge-in 的
   签名式失败，见 research/barge-in-two-layers.md）；
4. 记录已播放内容（ContextManager.record_interruption → heard/not_heard 分离）；
5. 发 AGENT_INTERRUPTED 域事件（metrics/UI 观测；barge-in latency 测量点）；
6. 发 PLAYBACK_STOPPED → 状态机 INTERRUPTED→LISTENING，重新听用户说。

C4 hook：min_barge_in_s 为最短打断时长参数——USER_SPEECH_STARTED 若携带
speech_duration_s 且低于阈值则跳过打断（记入 skipped）。正确的 backchannel
容忍需要媒体层在发事件前去抖；本参数仅留接线点（误判代价第一版可接受）。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from typing import Any, Protocol

from .context import ContextManager
from .events import Event, EventBus, EventType
from .state_machine import ConversationState, ConversationStateMachine


class PlaybackLike(Protocol):
    """runtime.playback.StreamingPlayer 的结构子集：stop() 返回已播秒数。"""

    def stop(self) -> float: ...


class TTSLike(Protocol):
    """TTSProvider 的结构子集：cancel 停推+清 buffer（可为协程）。"""

    def cancel(self) -> Any: ...


class InterruptionManager:
    """六步打断执行器；副作用走注入的 playback/tts/llm 句柄，纯逻辑可测。"""

    def __init__(
        self,
        bus: EventBus,
        machine: ConversationStateMachine,
        context: ContextManager,
        *,
        playback: PlaybackLike | None = None,
        tts: TTSLike | None = None,
        min_barge_in_s: float = 0.0,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._machine = machine
        self._ctx = context
        self._playback = playback
        self._tts = tts
        self.min_barge_in_s = min_barge_in_s
        self._now = now_fn
        self._llm_cancels: set[Callable[[], Any]] = set()
        #: 每次打断的记录（trigger/played_s/utterance_id/heard/not_heard/errors）。
        self.interruptions: list[dict[str, Any]] = []
        #: 被 min_barge_in_s hook 跳过的触发事件。
        self.skipped: list[Event] = []
        # 六步执行中屏蔽自身发布的嵌套事件（AGENT_INTERRUPTED/PLAYBACK_STOPPED
        # 回环到本 handler 时不重入，否则会二次执行）。
        self._executing = False
        for et in (
            EventType.USER_SPEECH_STARTED,
            EventType.AGENT_INTERRUPTED,
            EventType.SILENCE_REQUESTED,
        ):
            bus.subscribe(et, self._on_event)

    # ---- 在途 LLM 登记（pipeline 发起生成时 track，完成/取消时 untrack） ----

    def track_llm(self, cancel: Callable[[], Any]) -> Callable[[], Any]:
        """登记一个在途生成的 cancel 回调（如同步函数或 asyncio.Task.cancel）。"""
        self._llm_cancels.add(cancel)
        return cancel

    def untrack_llm(self, cancel: Callable[[], Any]) -> None:
        self._llm_cancels.discard(cancel)

    @property
    def llm_in_flight(self) -> bool:
        return bool(self._llm_cancels)

    # ---- 触发判定 ----

    def should_interrupt(self, event: Event) -> bool:
        """C4 hook：最短打断时长。payload 未带 speech_duration_s 时立即打断。"""
        duration = event.payload.get("speech_duration_s")
        if duration is None or self.min_barge_in_s <= 0:
            return True
        return duration >= self.min_barge_in_s

    def _on_event(self, event: Event) -> None:
        if self._executing:
            return  # 自身六步中的嵌套发布不重复触发
        if event.type == EventType.SILENCE_REQUESTED:
            # 用户要求安静：有在途 utterance 就打断；机器已进 SILENCED，
            # 末尾的 PLAYBACK_STOPPED 被幂等忽略，静默保持。
            if self._utterance_open():
                self._execute(trigger=str(event.type))
            return
        if self._machine.state == ConversationState.SILENCED:
            return  # 静默中不产生任何打断动作
        # min_barge_in_s 只 gate 原始 VAD 触发；AGENT_INTERRUPTED 是已确认的
        # 打断信号，不做时长裁决。
        if (
            event.type == EventType.USER_SPEECH_STARTED
            and not self.should_interrupt(event)
        ):
            self.skipped.append(event)
            return
        if self._utterance_open() or self._machine.state == ConversationState.INTERRUPTED:
            self._execute(trigger=str(event.type))

    # ---- 六步 ----

    def _execute(self, *, trigger: str) -> dict[str, Any]:
        self._executing = True
        try:
            return self._execute_steps(trigger=trigger)
        finally:
            self._executing = False

    def _execute_steps(self, *, trigger: str) -> dict[str, Any]:
        utterance = self._ctx.current_utterance
        errors: list[str] = []

        # 1. 停止音频播放：返回的 played_s 即"实际播到哪"（heard 的边界）。
        played_s = 0.0
        if self._playback is not None:
            try:
                played_s = self._playback.stop()
            except Exception as e:  # 停播失败不阻断后续步骤
                errors.append(f"playback.stop: {e}")

        # 2. 清空未播放 TTS buffer（cancel 契约含 buffer 清理与可重开）。
        if self._tts is not None:
            try:
                self._invoke(self._tts.cancel)
            except Exception as e:
                errors.append(f"tts.cancel: {e}")

        # 3. 取消在途 LLM（尝试取消：单个失败不影响其余）。
        cancels = list(self._llm_cancels)
        self._llm_cancels.clear()
        for cancel in cancels:
            try:
                self._invoke(cancel)
            except Exception as e:
                errors.append(f"llm.cancel: {e}")

        # 4. 记录已播放内容：heard / generated_but_not_heard 分离入双历史。
        self._ctx.record_interruption(played_s)

        record = {
            "trigger": trigger,
            "played_s": played_s,
            "utterance_id": utterance.id if utterance else None,
            "heard": utterance.heard if utterance else "",
            "not_heard": utterance.not_heard if utterance else "",
            "errors": errors,
        }
        self.interruptions.append(record)

        # 5. AGENT_INTERRUPTED 域通知；由本事件触发（非 AGENT_INTERRUPTED 自身）
        #    时才发，避免自我回声。机器对不适用触发幂等忽略。
        if trigger != str(EventType.AGENT_INTERRUPTED):
            self._bus.publish(EventType.AGENT_INTERRUPTED, dict(record))

        # 6. PLAYBACK_STOPPED → INTERRUPTED→LISTENING（doc §2.2 第 6 步）。
        self._bus.publish(
            EventType.PLAYBACK_STOPPED,
            {"played_s": played_s, "reason": "interrupted", "trigger": trigger},
        )
        return record

    # ---- 内部 ----

    def _utterance_open(self) -> bool:
        utterance = self._ctx.current_utterance
        return utterance is not None and utterance.open

    @staticmethod
    def _invoke(fn: Callable[[], Any]) -> None:
        """调用 cancel 类回调；返回 awaitable 时按 EventBus.subscribe_async
        同款语义调度：有 running loop 则 create_task，否则 asyncio.run。"""
        result = fn()
        if inspect.isawaitable(result):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(result)
            else:
                loop.create_task(result)
