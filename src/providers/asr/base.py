"""ASRProvider 抽象：音频流 → partial/final 文字事件。

权责边界（streaming-asr-zh C3）：ASR 只做信号源，事件面只有 partial/final，
不产出轮次判定。轮次裁决归 Conversation Core / Smart Turn，adapter 不得
把服务端断句（sentence_end 等）直接映射成 TURN_COMPLETE。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

ASREventKind = Literal["partial", "final"]


@dataclass(frozen=True)
class ASREvent:
    """一次 ASR 转写事件。

    kind:
        "partial" — 识别进行中的不稳定中间结果，可反复修正；
        "final"   — 一次断句落地的稳定文本。两者都只是信号，
                    不携带任何轮次语义。
    """

    kind: ASREventKind
    text: str
    # provider 原始负载（断句 id、时间戳等），供 metrics/调试使用
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in ("partial", "final"):
            raise ValueError(
                f"ASREvent.kind must be 'partial' or 'final', got {self.kind!r}"
            )


class ASRProvider(ABC):
    """流式 ASR 供应商抽象。

    实现方把 PCM 音频流送进 `stream()`，按到达顺序产出 ASREvent。
    业务层（Conversation Core）只消费 ASREvent，不感知具体协议。
    """

    @abstractmethod
    def stream(
        self,
        audio: AsyncIterable[bytes],
        *,
        sample_rate: int = 16000,
        **kwargs: Any,
    ) -> AsyncIterator[ASREvent]:
        """消费 PCM 音频流，产出 partial/final 事件序列。

        audio 结束时实现方应完成收尾（flush + 发剩余 final）后返回。
        """
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        """释放连接/会话资源。幂等。"""
        raise NotImplementedError
