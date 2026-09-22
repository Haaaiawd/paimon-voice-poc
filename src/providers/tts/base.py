"""TTSProvider 抽象：增量文本流 → 音频流 + 取消契约。

cancel 契约（chinese-tts-eval C3，barge-in 硬依赖，必须实测断言）：

1. 停推 —— cancel() 返回后，进行中的 stream_audio() 迭代不得再产出
   新的音频 chunk（应尽快结束迭代）；
2. buffer 清理 —— adapter 内部尚未交付的音频/文本缓冲必须丢弃，
   不得留到下一次合成串音；
3. 可重开 —— cancel() 之后立刻再次调用 stream_audio() 必须是干净的
   新合成，不允许残留上一句的音频或协议状态。

以上语义不允许"假设服务端会停推"，每个真实 adapter（TASK-008）都要带
对应断言的测试。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any


class TTSError(Exception):
    """TTS provider 侧错误：握手失败 / task-failed / 连接异常中断。"""


class TTSProvider(ABC):
    """流式 TTS 供应商抽象。

    输入是 Text Chunker 切好的语义边界文本块流，输出是音频字节流
    （编码格式由实现方在构造参数里固定）。
    """

    @abstractmethod
    def stream_audio(
        self,
        chunks: AsyncIterable[str],
        **kwargs: Any,
    ) -> AsyncIterator[bytes]:
        """按语义边界消费文本块，产出音频 chunk。

        chunks 结束即本轮文本完毕，实现方负责向服务端发收尾事件
        （flush / finish-task 等）并把剩余音频吐完后返回。
        """
        raise NotImplementedError

    @abstractmethod
    async def cancel(self) -> None:
        """打断当前合成：停推 + 清空本地 buffer + 允许立刻重开。

        见模块 docstring 的三条契约。无进行中合成时调用应是无害的 no-op。
        """
        raise NotImplementedError
