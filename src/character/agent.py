"""CharacterAgent：persona + BehaviorPolicy + LLM 结构化输出的接线点。

输入是 ContextManager.build_agent_input 产出的 doc 03 §5 结构；
输出是 doc 03 §6 的 AgentReply。`speech` 为空即 NOOP——主动开口场景
模型选择不说（doc 03 §2.3 的闭嘴能力），或 SILENCED 被闸口拦下
（doc 05 §6：此时不打 LLM，状态机侧 ASR 继续转写但不进 LLM）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from providers.llm.base import AgentReply, ChatMessage, LLMProvider

from .behavior_policy import BehaviorConstraints, BehaviorPolicy
from .persona import PAIMON, Persona
from .prompt import build_messages

#: 确定性闭嘴回复：speech 空 = NOOP（pipeline 侧据此跳过 TTS）。
NOOP_REPLY = AgentReply(speech="", emotion="neutral", energy=0.0)


def is_noop(reply: AgentReply) -> bool:
    """speech 为空视为 NOOP（doc 03 §2.3：LLM 仍可返回 NOOP）。"""
    return not reply.speech.strip()


class CharacterAgent:
    """派蒙发言决策层：doc 03 §5 输入 → §6 结构化输出。

    权责边界：只决定"轮到派蒙时说什么"；轮次判定、打断执行、
    SILENCED 进入/退出都不在这里（Conversation Core 的职责）。
    """

    def __init__(
        self,
        llm: LLMProvider,
        persona: Persona = PAIMON,
        policy: BehaviorPolicy | None = None,
    ) -> None:
        self.persona = persona
        self.policy = policy or BehaviorPolicy(persona)
        self._llm = llm

    def constraints_for(
        self, agent_input: Mapping[str, Any]
    ) -> BehaviorConstraints:
        """本轮行为约束（BehaviorPolicy 规则推导 + 显式覆盖）。"""
        return self.policy.derive(agent_input)

    def build_messages(
        self, agent_input: Mapping[str, Any]
    ) -> list[ChatMessage]:
        """doc 03 §5 输入 → chat messages（含六字槽 system prompt）。"""
        constraints = self.constraints_for(agent_input)
        return build_messages(self.persona, agent_input, constraints)

    async def respond(
        self, agent_input: Mapping[str, Any], **kwargs: Any
    ) -> AgentReply:
        """打一次结构化 LLM，返回 AgentReply；SILENCED 直接 NOOP。"""
        constraints = self.constraints_for(agent_input)
        if not constraints.may_speak:
            return NOOP_REPLY
        messages = build_messages(self.persona, agent_input, constraints)
        return await self._llm.complete_structured(messages, **kwargs)
