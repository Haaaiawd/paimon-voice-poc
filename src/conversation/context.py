"""ContextManager：logical history 与 heard history 双历史（doc 03 §2.4/§3/§5）。

- Logical History：模型实际生成了什么（完整 generated text，含未播出部分）。
- Heard History：用户实际听到了什么（只记播出部分；被打断轮次带标记）。

被打断的轮次在下一轮 LLM 输入中以 interruption_context 呈现（doc 03 §3）：
{assistant_heard, assistant_generated_but_not_heard, user,
 event="assistant_was_interrupted"}——不能把未播出的生成内容写进普通聊天
历史，否则模型会以为用户听到了事实上没听到的内容（turn-taking C5）。

utterance 生命周期：begin_utterance() → add_generated()/add_segment() →
自然播完（PLAYBACK_STOPPED 自动封盘，heard=全部已交付文本）或由
InterruptionManager 调 record_interruption（按 played_s 截断 heard）。
played_s→字符的映射用分段时长线性估计：纯逻辑层的最佳近似；真实接线时
played_s 来自 playback.position_seconds / stop()（runtime/playback.py）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .events import Event, EventBus, EventType

#: doc 03 §3 的截断标记与事件名（验收点：原样呈现）。
TRUNCATION_MARK = "——"
ASSISTANT_INTERRUPTED_EVENT = "assistant_was_interrupted"


@dataclass
class SpokenSegment:
    """已交付 TTS/播放的一段文本与其音频时长。"""

    text: str
    audio_s: float


@dataclass
class AgentUtterance:
    """一次 agent 出声：generated 为 logical 面，segments/heard 为 heard 面。

    - generated：LLM 已产出的全文（打断时可能还在增长，以封盘时为准）；
    - segments：已交给 TTS/播放器的文本块及其音频时长；
    - heard/not_heard：封盘时按 played_s 算出的字符级切分。
    """

    id: int
    generated: str = ""
    segments: list[SpokenSegment] = field(default_factory=list)
    open: bool = True
    interrupted: bool = False
    played_s: float = 0.0
    heard: str = ""
    not_heard: str = ""

    @property
    def spoken_text(self) -> str:
        return "".join(s.text for s in self.segments)

    @property
    def audio_s(self) -> float:
        return sum(s.audio_s for s in self.segments)

    def add_generated(self, text: str) -> None:
        self.generated += text

    def add_segment(self, text: str, audio_s: float) -> None:
        self.segments.append(SpokenSegment(text, max(0.0, audio_s)))

    def heard_char_count(self, played_s: float) -> int:
        """played_s 映射到 spoken 文本的字符数（分段时长线性估计）。"""
        remaining = max(0.0, played_s)
        count = 0
        for seg in self.segments:
            if seg.audio_s <= 0:
                count += len(seg.text)  # 零时长段视为瞬时播完
                continue
            if remaining >= seg.audio_s:
                remaining -= seg.audio_s
                count += len(seg.text)
                continue
            count += int(len(seg.text) * (remaining / seg.audio_s))
            break
        return count

    def heard_display(self) -> str:
        """doc 03 §3 的 heard 呈现：被打断且还有未播内容时以 —— 标记截断。"""
        if self.interrupted and self.heard and self.not_heard:
            return self.heard + TRUNCATION_MARK
        return self.heard


class ContextManager:
    """双历史与 doc 03 §5 Agent 输入构造。

    - `logical_history` / `heard_history`：{role, text, ...} 条目序列；
      user 轮次两边一致，assistant 轮次 logical=generated、heard=播出部分。
    - `pending_interruption`：最近一次打断的 §3 结构，build_agent_input
      一次性消费（只影响"下一轮"LLM 输入）。
    - 传 `bus` 则订阅 PLAYBACK_STOPPED 自动封盘当前 utterance（自然播完）。
    """

    def __init__(
        self,
        bus: EventBus | None = None,
        *,
        character: str = "paimon",
        history_limit: int = 20,
    ) -> None:
        self.character = character
        self.history_limit = history_limit
        self.logical_history: list[dict[str, Any]] = []
        self.heard_history: list[dict[str, Any]] = []
        self.utterances: list[AgentUtterance] = []
        self.current_utterance: AgentUtterance | None = None
        self.pending_interruption: dict[str, Any] | None = None
        self._next_id = 0
        if bus is not None:
            bus.subscribe(EventType.PLAYBACK_STOPPED, self._on_playback_stopped)

    # ---- utterance 生命周期（pipeline 在 LLM/TTS 接线时调用） ----

    def begin_utterance(self) -> AgentUtterance:
        """新一轮 agent 出声开始；返回句柄供累加 generated/segments。"""
        self._next_id += 1
        utterance = AgentUtterance(id=self._next_id)
        self.utterances.append(utterance)
        self.current_utterance = utterance
        return utterance

    def record_user_turn(self, text: str, *, turn_id: int | None = None) -> None:
        """用户轮次落双历史（用户说的两边一致）。"""
        entry: dict[str, Any] = {"role": "user", "text": text}
        if turn_id is not None:
            entry["turn_id"] = turn_id
        self.logical_history.append(entry)
        self.heard_history.append(dict(entry))

    # ---- 封盘 ----

    def seal_current(self, played_s: float | None = None) -> AgentUtterance | None:
        """自然播完封盘：heard=全部已交付文本，不产出 interruption context。"""
        utterance = self.current_utterance
        if utterance is None or not utterance.open:
            return utterance
        utterance.open = False
        utterance.played_s = utterance.audio_s if played_s is None else played_s
        utterance.heard = utterance.spoken_text
        utterance.not_heard = utterance.generated[len(utterance.heard) :]
        if utterance.generated:
            # 全空 utterance（NOOP 轮）不入历史：没有内容可记。
            self._commit(utterance)
        self.current_utterance = None
        return utterance

    def record_interruption(
        self,
        played_s: float,
        utterance: AgentUtterance | None = None,
    ) -> dict[str, Any] | None:
        """打断封盘：按 played_s 截断 heard，产出 doc 03 §3 的 pending context。

        heard 只到已播位置；generated_but_not_heard 覆盖"已合成未播"与
        "已生成未合成"两部分——模型不许假设用户听到了它们。
        """
        utterance = utterance or self.current_utterance
        if utterance is None or not utterance.open:
            return None
        if not utterance.generated:
            # 出声前就被掐掉且未生成任何文本（如 initiative 响应被用户开口
            # 取消）：静默封盘，不入双历史也不产 interruption context——
            # 用户视角派蒙什么都没说。
            utterance.open = False
            if utterance is self.current_utterance:
                self.current_utterance = None
            return None
        utterance.open = False
        utterance.interrupted = True
        utterance.played_s = max(0.0, played_s)
        k = utterance.heard_char_count(utterance.played_s)
        utterance.heard = utterance.spoken_text[:k]
        utterance.not_heard = utterance.generated[len(utterance.heard) :]
        self._commit(utterance)
        if utterance is self.current_utterance:
            self.current_utterance = None
        self.pending_interruption = {
            "assistant_heard": utterance.heard_display(),
            "assistant_generated_but_not_heard": utterance.not_heard,
            "user": "",  # 打断轮次的转写在 build 时填入（doc §3 的 user 行）
            "event": ASSISTANT_INTERRUPTED_EVENT,
        }
        return self.pending_interruption

    # ---- Agent 输入 ----

    def build_agent_input(
        self,
        *,
        state: Any,
        last_user_text: str = "",
        silence_duration_ms: float = 0,
        initiative_reason: str | None = None,
        behavior_constraints: dict[str, Any] | None = None,
        character: str | None = None,
        consume_interruption: bool = True,
    ) -> dict[str, Any]:
        """doc 03 §5 的 Agent 输入结构；pending interruption 一次性消费。

        `consume_interruption=False` 用于投机预构造：pipeline 在 ASR partial
        到达时先 peek interruption context 构造 prompt，正式提交时才消费，
        避免投机 miss 把上下文白白丢掉。
        """
        interruption = self.pending_interruption
        if consume_interruption:
            self.pending_interruption = None
        if interruption is not None:
            interruption["user"] = last_user_text
        return {
            "character": character or self.character,
            "state": str(state),
            "last_user_text": last_user_text,
            "recent_heard_history": list(
                self.heard_history[-self.history_limit :]
            ),
            "interruption_context": interruption,
            "silence_duration_ms": int(silence_duration_ms),
            "initiative_reason": initiative_reason,
            "behavior_constraints": dict(behavior_constraints or {}),
        }

    # ---- 内部 ----

    def _commit(self, utterance: AgentUtterance) -> None:
        """封盘写入双历史：logical 记完整 generated，heard 记播出部分。"""
        self.logical_history.append(
            {
                "role": "assistant",
                "text": utterance.generated,
                "heard_text": utterance.heard,
                "interrupted": utterance.interrupted,
                "utterance_id": utterance.id,
            }
        )
        self.heard_history.append(
            {
                "role": "assistant",
                "text": utterance.heard,
                "interrupted": utterance.interrupted,
                "utterance_id": utterance.id,
            }
        )

    def _on_playback_stopped(self, event: Event) -> None:
        # 打断路径已在 record_interruption 封盘（open=False），此处只收自然播完。
        self.seal_current(played_s=event.payload.get("played_s"))
