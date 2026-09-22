"""System prompt 与 messages 构造（doc 05 §10 / doc 03 §3、§5）。

doc 05 §10：System Prompt 不写角色小说。渲染按 PRISMIX 三层组织——
stable core（身份 + 气质，来自 Persona，不随轮次变化）→
environment adaptation（语音输出约束 + 长度基线 + 输出契约）→
current turn（本轮动态行为限制：被打断 / 主动开口 / SILENCED，
命中才渲染；SILENCED 为防御行，正常链路在 agent 层已被闸口拦下）。
每个槽位一条独立规则行，无叙事段落；规则只保留能改变行为的句子。

doc 03 §5 的 agent input 经 `build_messages` 渲染：recent_heard_history
进 user/assistant 角色位（heard 面，被打断轮次带 —— 截断标记），
本轮输入按 §3 的键值块收进最后一条 user 消息——interruption_context
原样呈现 assistant_heard / generated_but_not_heard / event，
模型不许假设用户听到了未播出部分（turn-taking C5）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from providers.llm.base import ChatMessage

from .behavior_policy import BehaviorConstraints
from .persona import EMOTION_TAG_ORDER, Persona

#: doc 03 §3 的截断标记（与 conversation.context 同一约定）。
TRUNCATION_MARK = "——"


def build_system_prompt(
    persona: Persona,
    constraints: BehaviorConstraints,
    memory: str = "",
) -> str:
    """渲染分层 system prompt；每个槽位一条规则行。"""
    lines = [
        # ── stable core：身份与人格价值 ──
        f"你是{persona.identity}。",
        f"语气：{'；'.join(persona.tone)}；吐槽要准，但别真伤人；"
        "专注当下——记忆只在对方提起或很贴切时才用，别主动翻旧账。",
        # 记忆槽位：stable core 数据层，与人格并排常驻；规则在语气行，
        # 这里是"她记得什么"而不是"怎么用"。无记忆配置则不渲染。
        *([f"记忆：{memory}"] if memory else []),
        # ── environment adaptation：实时语音约束 ──
        # speech 会被 TTS 逐字念出，排版/符号类输出是真实失败模式
        "语音：speech 会被 TTS 直接念出来——口语短句，"
        "不要列表、markdown、emoji、括号注释；"
        "不复读对方原话，也不重复自己刚说过的话；没听清就吐槽没听清。",
        (
            f"长度：平时 {constraints.max_sentences} 句以内，"
            f"对方明确要详细解释时最多 {persona.long_max_sentences} 句，"
            "判断归你；直接回应对方最后一句；对方只是嗯啊之类的碎话，"
            "随口接一句或不说，别重讲前面的话题。"
        ),
        # ── capability module：输出契约与闭嘴能力（doc 03 §6 schema；
        # emotion 枚举 = doc 05 §8）──
        '只输出一个 JSON 对象 {"speech": string, "emotion": '
        + "|".join(EMOTION_TAG_ORDER)
        + ' 之一, "energy": 0到1的小数, "should_continue": bool}；'
        "不要输出任何其他文字。无话可说时 speech 为空字符串。",
    ]
    # ── current turn：本轮行为限制（动态槽位，命中才渲染）──
    if constraints.was_interrupted:
        lines.append(
            "你上一句被打断了：不要自动补完原句，按上下文放弃、"
            "换一句、认怂，或吐槽一句。"
        )
    if constraints.is_initiative:
        lines.append("这次是你主动开口：没有值得说的就让 speech 为空。")
    if not constraints.may_speak:
        lines.append("用户要求你安静：speech 返回空字符串。")
    return "\n".join(lines)


def render_turn_input(agent_input: Mapping[str, Any]) -> str:
    """doc 03 §5 本轮输入 → 键值行块（最后一条 user 消息的正文）。"""
    lines = [f"state: {agent_input.get('state')}"]
    silence_ms = agent_input.get("silence_duration_ms")
    if silence_ms:
        lines.append(f"silence_duration_ms: {int(silence_ms)}")
    reason = agent_input.get("initiative_reason")
    if reason is not None:
        lines.append(f'initiative_reason: "{reason}"')
    interruption = agent_input.get("interruption_context")
    if interruption:
        # doc 03 §3 原样四行：heard / not_heard / event / user
        lines.append(
            f'assistant_heard: "{interruption.get("assistant_heard", "")}"'
        )
        lines.append(
            'assistant_generated_but_not_heard: '
            f'"{interruption.get("assistant_generated_but_not_heard", "")}"'
        )
        lines.append(f"event: {interruption.get('event')}")
    user_text = str(agent_input.get("last_user_text") or "")
    lines.append(f'user: "{user_text}"')
    return "\n".join(lines)


def build_messages(
    persona: Persona,
    agent_input: Mapping[str, Any],
    constraints: BehaviorConstraints,
) -> list[ChatMessage]:
    """system + heard history（角色位）+ 本轮输入块。"""
    messages: list[ChatMessage] = [
        {
            "role": "system",
            "content": build_system_prompt(
                persona, constraints, memory=str(agent_input.get("memory") or "")
            ),
        }
    ]
    for entry in agent_input.get("recent_heard_history") or ():
        text = str(entry.get("text") or "")
        if not text:
            continue
        # heard 面历史：被打断的 assistant 轮次补截断标记
        if entry.get("role") == "assistant" and entry.get("interrupted"):
            text += TRUNCATION_MARK
        messages.append({"role": str(entry.get("role")), "content": text})
    messages.append({"role": "user", "content": render_turn_input(agent_input)})
    return messages
