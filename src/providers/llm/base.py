"""LLMProvider 抽象 + 派蒙结构化输出（doc 03 §5/§6）。

LLM 只负责"轮到派蒙时说什么"，不判断轮次、不碰音频状态（02 §4）。
赛马统一走 OpenAI-compatible（D-004），最重要指标是 TTFT。
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# 05_PAIMON_PERSONA.md / paimon-persona.md 的首版情绪标签集；
# 未识别标签按 chinese-tts-eval C5 降级为 neutral。
EMOTION_TAGS = frozenset(
    {
        "neutral",
        "happy",
        "excited",
        "teasing",
        "annoyed",
        "confused",
        "smug",
        "soft",
    }
)

ChatMessage = Mapping[str, Any]


class LLMError(Exception):
    """LLM 调用失败（网络、HTTP 错误、协议异常）。"""


class StructuredOutputError(LLMError):
    """模型输出无法解析为结构化 AgentReply。"""


@dataclass(frozen=True)
class AgentReply:
    """doc 03 §6 的结构化输出：speech/emotion/energy/should_continue。"""

    speech: str
    emotion: str = "neutral"
    energy: float = 0.5
    should_continue: bool = False


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

#: 兜底打捞：JSON 结构损坏但 speech 字段完整时按字段取值。
_SPEECH_FIELD_RE = re.compile(r'"speech"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _salvage_speech(text: str) -> str | None:
    """从坏 JSON 里捞 "speech": "..."——结构坏了但值完整时仍可用。"""
    m = _SPEECH_FIELD_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(f'"{m.group(1)}"')
    except json.JSONDecodeError:
        return m.group(1)


def _extract_json(text: str) -> str:
    """容忍 ```json 围栏与前后多余文字，取出最外层 JSON 对象。"""
    fenced = _FENCE_RE.search(text)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise StructuredOutputError(f"no JSON object in LLM output: {text!r}")
    return candidate[start : end + 1]


def parse_agent_reply(text: str) -> AgentReply:
    """把模型输出解析为 AgentReply，兼容结构化对象与 JSON 字符串降级。"""
    try:
        direct = json.loads(text.strip())
    except json.JSONDecodeError:
        direct = None
    if isinstance(direct, str):
        return AgentReply(speech=direct)
    if isinstance(direct, list):
        speech = "".join(item for item in direct if isinstance(item, str))
        return AgentReply(speech=speech)

    try:
        data = json.loads(_extract_json(text))
    except (json.JSONDecodeError, StructuredOutputError) as e:
        salvaged = _salvage_speech(text)
        if salvaged is not None:
            return AgentReply(speech=salvaged)
        raise StructuredOutputError(f"invalid JSON in LLM output: {e}") from e
    if not isinstance(data, dict):
        raise StructuredOutputError(f"LLM output is not a JSON object: {data!r}")

    emotion = str(data.get("emotion") or "neutral")
    try:
        energy = float(data.get("energy", 0.5))
    except (TypeError, ValueError):
        energy = 0.5
    return AgentReply(
        speech=str(data.get("speech") or ""),
        emotion=emotion if emotion in EMOTION_TAGS else "neutral",
        energy=min(max(energy, 0.0), 1.0),
        should_continue=bool(data.get("should_continue", False)),
    )


class LLMProvider(ABC):
    """LLM 供应商抽象。

    - stream_reply：token 级流式输出，喂 Text Chunker（02 §2）；
    - complete_structured：JSON 模式收完整结构化结果（doc 03 §6）。
    所有实现必须可取消：外部取消消费迭代即中止请求，不留后台任务。
    """

    @abstractmethod
    def stream_reply(
        self,
        messages: Sequence[ChatMessage],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """流式产出回复 token。"""
        raise NotImplementedError

    @abstractmethod
    async def complete_structured(
        self,
        messages: Sequence[ChatMessage],
        **kwargs: Any,
    ) -> AgentReply:
        """以结构化输出模式取完整 AgentReply。"""
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        """释放底层连接。幂等。"""
        raise NotImplementedError
