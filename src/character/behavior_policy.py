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


@dataclass(frozen=True)
class BehaviorConstraints:
    """一轮回复的行为约束（doc 05 §10 的动态槽位来源）。

    - max_sentences：本轮长度基线（上限在 prompt 里给，判断归模型）；
    - was_interrupted：上一句被打断（doc 05 §5：不自动补完原句）；
    - is_initiative：本轮由 InitiativePolicy 触发（主动开口，允许 NOOP）；
    - may_speak：False 时 agent 层直接 NOOP，不打 LLM（doc 05 §6 SILENCED）；
    - may_noop：允许返回空 speech（主动开口"少而准"的闭嘴能力）。
    """

    max_sentences: int
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

        constraints = BehaviorConstraints(
            max_sentences=self.persona.default_max_sentences,
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
