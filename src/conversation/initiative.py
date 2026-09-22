"""InitiativePolicy + SilenceClassifier：doc 03 §2.3 主动性评分与 doc 05 §6
"用户要求闭嘴"的规则面落地（design conversation-core.md "SILENCED 的生产者
与退出"，D-013）。

两个组件都在 Conversation Core 内、纯事件驱动、不经过 LLM：

- `SilenceClassifier` 消费 ASR_FINAL 文本：命中"闭嘴/别说话/安静"类指令 →
  发 SILENCE_REQUESTED（可带解析出的 duration_s，如"闭嘴两分钟"）；状态机
  全局迁移进 SILENCED。SILENCED 中 ASR 继续转写（本轮次照常裁决但不发
  AGENT_CAN_RESPOND——TurnManager 已挡），final 含"派蒙"直呼 →
  machine.wake() 回 IDLE；超时退出由状态机 tick 的 silence_window 兜底。
- `InitiativePolicy` 实现 §2.3 可解释评分：silence_duration +
  interesting_context + direct_mention + conversation_energy
  − recently_spoke；硬规则 user_is_speaking / silenced → NEVER_SPEAK
  （本实现同样门控 THINKING/SPEAKING/INTERRUPTED——在途响应不叠加主动
  开口）。另设 min_silence_s 地板：没有"明显留白"永远不开口（D-008：
  第一版不抢用户话头）。达阈值只发 INITIATIVE_TRIGGERED——"允许问一次"，

  说不说由 LLM 定（可 NOOP），触发后进入 cooldown 防连环插话。

参数全部是 InitativeConfig 字段（doc 07 Q-005：先规则、实测后调）。
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .events import Event, EventBus, EventType
from .state_machine import ConversationState, ConversationStateMachine
from .turn_manager import TurnManager

# ---------------------------------------------------------------- 评分配置


@dataclass(frozen=True)
class InitiativeConfig:
    """§2.3 公式的权重与时间尺度。默认值是"先能跑"的保守档：

    - 冷场 30s 拿满 silence 权重 = threshold——纯沉默 30s 可触发一次；
    - mention/interesting/energy 只能加速触发，不能单独越过 min_silence；
    - 派蒙刚说完（recently_spoke 20s 内）显著压分。
    """

    threshold: float = 1.0
    min_silence_s: float = 6.0  # 明显留白地板：低于此任何组合都不开口
    cooldown_s: float = 45.0  # 两次主动开口最小间隔（NOOP 同样计）
    silence_full_s: float = 30.0
    w_silence: float = 1.0
    w_interesting: float = 0.4
    w_mention: float = 0.6
    mention_window_s: float = 15.0
    w_energy: float = 0.3
    energy_window_s: float = 60.0
    energy_full_turns: int = 3
    w_recently_spoke: float = 0.8
    recently_spoke_decay_s: float = 20.0


#: 会话活动的语义快照：evaluate() 的输入面。
@dataclass
class _Tracks:
    last_activity: float = 0.0  # 最近一次"有声"事件（用户说/派蒙说/轮次）
    last_agent_spoke: float | None = None
    mention_ts: float | None = None  # 最近 ASR final 含"派蒙"
    interesting: bool = False  # 有未接住的话头（派蒙上一句被打断）
    turn_ts: deque[float] = field(default_factory=deque)


class InitiativePolicy:
    """§2.3 评分器：订阅 bus 维护信号面，tick() 评估并按需发 INITIATIVE_TRIGGERED。"""

    #: 硬规则命中的原因标签（观测/测试用）。
    NEVER_SPEAK_REASONS = (
        "user_is_speaking",
        "silenced",
        "response_in_flight",
        "cooldown",
        "min_silence",
    )

    def __init__(
        self,
        bus: EventBus,
        machine: ConversationStateMachine,
        turn_manager: TurnManager,
        *,
        config: InitiativeConfig | None = None,
        now_fn: Callable[[], float] = time.monotonic,
        wake_words: tuple[str, ...] = ("派蒙",),
    ) -> None:
        self._bus = bus
        self._machine = machine
        self._tm = turn_manager
        self.config = config or InitiativeConfig()
        self._now = now_fn
        self._wake_words = wake_words
        self._t = _Tracks(last_activity=now_fn())
        self._cooldown_until = 0.0
        for et in (
            EventType.USER_SPEECH_STARTED,
            EventType.USER_SPEECH_STOPPED,
            EventType.USER_TURN_COMPLETE,
            EventType.ASR_FINAL,
            EventType.AGENT_SPEAKING,
            EventType.AGENT_INTERRUPTED,
            EventType.PLAYBACK_STOPPED,
        ):
            bus.subscribe(et, self._on_event)

    # ---- 信号面维护 ----

    def _on_event(self, event: Event) -> None:
        t = self._t
        match event.type:
            case EventType.USER_SPEECH_STARTED | EventType.USER_SPEECH_STOPPED:
                t.last_activity = event.ts
            case EventType.USER_TURN_COMPLETE:
                t.last_activity = event.ts
                t.turn_ts.append(event.ts)
            case EventType.ASR_FINAL:
                t.last_activity = event.ts
                text = event.payload.get("text") or ""
                if any(w in text for w in self._wake_words):
                    t.mention_ts = event.ts
            case EventType.AGENT_SPEAKING:
                t.last_activity = event.ts
                t.last_agent_spoke = event.ts
                t.interesting = False  # 话头已接住
            case EventType.AGENT_INTERRUPTED:
                t.last_activity = event.ts
                t.interesting = True  # 被打断的派蒙句 = "上一轮留下的梗"
            case EventType.PLAYBACK_STOPPED:
                t.last_activity = event.ts

    # ---- 硬规则（NEVER_SPEAK） ----

    def gate_reason(self, now: float) -> str | None:
        """None = 允许进入评分；否则返回 NEVER_SPEAK 原因。"""
        cfg = self.config
        state = self._machine.state
        if state == ConversationState.SILENCED:
            return "silenced"
        if self._tm.turn_open or state in (
            ConversationState.LISTENING,
            ConversationState.POSSIBLE_END,
        ):
            return "user_is_speaking"
        if state in (
            ConversationState.THINKING,
            ConversationState.SPEAKING,
            ConversationState.INTERRUPTED,
        ):
            return "response_in_flight"
        if now < self._cooldown_until:
            return "cooldown"
        if now - self._t.last_activity < cfg.min_silence_s:
            return "min_silence"
        return None

    # ---- 评分 ----

    def evaluate(self, now: float | None = None) -> dict[str, Any]:
        """§2.3 公式求值。返回 {score, gate, breakdown, silence_s}；
        gate 非 None 时 score 无意义（NEVER_SPEAK）。"""
        now = self._now() if now is None else now
        cfg = self.config
        gate = self.gate_reason(now)
        silence_s = max(0.0, now - self._t.last_activity)
        if gate is not None:
            return {
                "score": None,
                "gate": gate,
                "silence_s": round(silence_s, 3),
                "breakdown": {},
            }

        silence = cfg.w_silence * min(silence_s / cfg.silence_full_s, 1.0)
        interesting = cfg.w_interesting if self._t.interesting else 0.0
        mention = (
            cfg.w_mention
            if self._t.mention_ts is not None
            and now - self._t.mention_ts <= cfg.mention_window_s
            else 0.0
        )
        while self._t.turn_ts and (
            now - self._t.turn_ts[0] > cfg.energy_window_s
        ):
            self._t.turn_ts.popleft()
        energy = cfg.w_energy * min(
            len(self._t.turn_ts) / max(cfg.energy_full_turns, 1), 1.0
        )
        recent = 0.0
        if self._t.last_agent_spoke is not None:
            ago = now - self._t.last_agent_spoke
            recent = cfg.w_recently_spoke * max(
                0.0, 1.0 - ago / cfg.recently_spoke_decay_s
            )
        breakdown = {
            "silence_duration": round(silence, 3),
            "interesting_context": interesting,
            "direct_mention": mention,
            "conversation_energy": round(energy, 3),
            "recently_spoke": round(-recent, 3),
        }
        return {
            "score": round(
                silence + interesting + mention + energy - recent, 3
            ),
            "gate": None,
            "silence_s": round(silence_s, 3),
            "breakdown": breakdown,
        }

    # ---- 触发 ----

    def tick(self, now: float | None = None) -> bool:
        """由 ConversationCore.tick 驱动；达阈值发 INITIATIVE_TRIGGERED。

        触发即消费：mention/interesting 清零、沉默基准重置、进入 cooldown——
        不管 LLM 最后说没说（NOOP），都不连环追问。
        """
        now = self._now() if now is None else now
        result = self.evaluate(now)
        if result["gate"] is not None or result["score"] is None:
            return False
        if result["score"] < self.config.threshold:
            return False
        breakdown = result["breakdown"]
        reason = max(
            breakdown,
            key=lambda k: breakdown[k],
            default="long_silence",
        )
        self._bus.publish(
            EventType.INITIATIVE_TRIGGERED,
            {
                "reason": reason,
                "score": result["score"],
                "silence_ms": int(result["silence_s"] * 1000),
                "breakdown": breakdown,
            },
        )
        self._cooldown_until = now + self.config.cooldown_s
        self._t.last_activity = now
        self._t.mention_ts = None
        self._t.interesting = False
        return True


# ---------------------------------------------------------------- 闭嘴/唤醒


#: "闭嘴"类指令的 v1 匹配表（可配置；确定性规则不走 LLM，见 D-013）。
DEFAULT_SILENCE_PATTERNS: tuple[str, ...] = (
    "闭嘴",
    "住嘴",
    "住口",
    "别说话",
    "别说了",
    "先别说",
    "不要说话",
    "不用说话",
    "别再说了",
    "别出声",
    "别吵",
    "安静一下",
    "安静一会",
    "安静点",
    "安静会儿",
)

#: 否定/反指令豁免："别闭嘴""不许住口"是让派蒙继续说，不是静默指令。
NEGATION_GUARDS: tuple[str, ...] = (
    "不要闭嘴",
    "别闭嘴",
    "不许闭嘴",
    "不用闭嘴",
    "不要住嘴",
    "别住嘴",
)

DEFAULT_WAKE_WORDS: tuple[str, ...] = ("派蒙",)

#: "闭嘴两分钟"/"安静30秒"/"半个小时别说话" → duration_s。
_DURATION_RE = re.compile(
    r"(半|一|二|两|三|四|五|六|七|八|九|十|\d+(?:\.\d+)?)\s*"
    r"(分钟|分|个小时|小时|秒)"
)
_CN_NUM = {
    "半": 0.5,
    "一": 1.0,
    "二": 2.0,
    "两": 2.0,
    "三": 3.0,
    "四": 4.0,
    "五": 5.0,
    "六": 6.0,
    "七": 7.0,
    "八": 8.0,
    "九": 9.0,
    "十": 10.0,
}
_UNIT_S = {"秒": 1.0, "分": 60.0, "分钟": 60.0, "个小时": 3600.0, "小时": 3600.0}


def parse_silence_duration(text: str) -> float | None:
    """从指令文本解析静默时长（秒）；无显式时长返回 None（用默认窗口）。"""
    m = _DURATION_RE.search(text)
    if m is None:
        return None
    raw = m.group(1)
    num = _CN_NUM.get(raw) if not raw[0].isdigit() else None
    if num is None:
        try:
            num = float(raw)
        except ValueError:
            return None
    return num * _UNIT_S[m.group(2)]


class SilenceClassifier:
    """ASR_FINAL → SILENCE_REQUESTED 的规则分类器 + SILENCED 唤醒闸。

    - 非 SILENCED：文本命中静默指令 → 发 SILENCE_REQUESTED（duration_s 可选）；
    - SILENCED：静默指令重申保持沉默；"派蒙"直呼 → machine.wake() 退出。
      （唤醒检测只看 ASR_FINAL——ASR 在静默期持续转写，见 design 文档。）
    """

    def __init__(
        self,
        bus: EventBus,
        machine: ConversationStateMachine,
        *,
        silence_patterns: tuple[str, ...] = DEFAULT_SILENCE_PATTERNS,
        wake_words: tuple[str, ...] = DEFAULT_WAKE_WORDS,
    ) -> None:
        self._bus = bus
        self._machine = machine
        self.silence_patterns = silence_patterns
        self.wake_words = wake_words
        bus.subscribe(EventType.ASR_FINAL, self._on_asr_final)

    def is_silence_command(self, text: str) -> bool:
        if any(g in text for g in NEGATION_GUARDS):
            return False
        return any(p in text for p in self.silence_patterns)

    def _on_asr_final(self, event: Event) -> None:
        text = event.payload.get("text") or ""
        if not text:
            return
        if self._machine.state == ConversationState.SILENCED:
            # 静默期再说"继续闭嘴"：保持沉默（v1 不续期窗口）。
            if self.is_silence_command(text):
                return
            if any(w in text for w in self.wake_words):
                self._machine.wake()
            return
        if self.is_silence_command(text):
            payload: dict[str, Any] = {"text": text}
            duration = parse_silence_duration(text)
            if duration is not None:
                payload["duration_s"] = duration
            self._bus.publish(EventType.SILENCE_REQUESTED, payload)
