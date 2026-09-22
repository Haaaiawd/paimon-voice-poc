"""MemoryProvider 协议：对话核心只认这个接口，记忆实现可替换。

- recall(query)：按本轮用户输入检索相关记忆 → 注入 system prompt
  记忆槽位；返回 None 表示本轮没有贴切记忆。
- record(user_text, assistant_text)：轮末把已交付的对话写回记忆层；
  实现必须非阻塞（写路径打 LLM/embedding，绝不能堵 event loop）。

MVP 默认路径不走 provider——fixture 摘要常驻 system prompt；
MEMORY_PROVIDER=mem0 时换成 mem0 向量检索实现。
"""

from __future__ import annotations

from typing import Protocol


class MemoryProvider(Protocol):
    async def recall(self, query: str) -> str | None: ...

    def record(self, user_text: str, assistant_text: str) -> None: ...
