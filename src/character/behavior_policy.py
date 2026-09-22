"""Behavior Policy：doc 05 §3–§7 的规则面第一版。

把 doc 03 §5 的 agent input 翻译成本轮行为约束（BehaviorConstraints）：
prompt 层据此渲染"当前行为限制"槽位，agent 层据此做确定性闸口
（SILENCED 不打 LLM，直接 NOOP）。

第一版全部是可解释规则（doc 07 / Q-005：主动性参数先规则、实测后调；
TASK-009 boundary 明确不做自动调优）。agent_input["behavior_constraints"]
里的显式键优先于推导结果——pipeline 侧（TASK-010/011）可逐字段覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .persona import PAIMON, Persona

#: doc 05 §3："用户明确要求解释，再变长"。第一版用显式请求词规则判定；
#: 覆盖不足时调词表，不引入分类模型。
EXPLAIN_MARKERS: tuple[str, ...] = (
    "解释",
    "为什么",
    "为何",
    "详细",
    "讲讲",
    "讲一下",
    "是什么",
    "怎么回事",
    "怎么办",
    "教我",
)


@dataclass(frozen=True)
class BehaviorConstraints:
    """一轮回复的行为约束（doc 05 §10 的动态槽位来源）。

    - max_sentences：本轮长度上限；
    - long_answer：用户明确要求解释，长度放宽；
    - was_interrupted：上一句被打断（doc 05 §5：不自动补完原句）；
    - is_initiative：本轮由 InitiativePolicy 触发（主动开口，允许 NOOP）；
    - may_speak：False 时 agent 层直接 NOOP，不打 LLM（doc 05 §6 SILENCED）；
    - may_noop：允许返回空 speech（主动开口"少而准"的闭嘴能力）。
    """

    max_sentences: int
    long_answer: bool = False
    was_interrupted: bool = False
    is_initiative: bool = False
    may_speak: bool = True
    may_noop: bool = False


class BehaviorPolicy:
    """doc 03 §5 agent input → BehaviorConstraints 的规则推导。"""

    def __init__(self, persona: Persona = PAIMON) -> None:
        self.persona = persona

    def derive(self, agent_input: Mapping[str, Any]) -> BehaviorConstraints:
        state = str(agent_input.get("state") or "")
        silenced = state == "SILENCED"
        last_user_text = str(agent_input.get("last_user_text") or "")
        long_answer = self._wants_explanation(last_user_text)

        constraints = BehaviorConstraints(
            max_sentences=(
                self.persona.long_max_sentences
                if long_answer
                else self.persona.default_max_sentences
            ),
            long_answer=long_answer,
            was_interrupted=bool(agent_input.get("interruption_context")),
            is_initiative=agent_input.get("initiative_reason") is not None,
            may_speak=not silenced,
            may_noop=agent_input.get("initiative_reason") is not None,
        )
        overrides = agent_input.get("behavior_constraints") or {}
        if overrides:
            constraints = BehaviorConstraints(
                **{**constraints.__dict__, **dict(overrides)}
            )
        return constraints

    @staticmethod
    def _wants_explanation(text: str) -> bool:
        return any(marker in text for marker in EXPLAIN_MARKERS)
