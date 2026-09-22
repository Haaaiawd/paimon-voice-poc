"""长期记忆 fixture 装载（MVP：启动时全量摘要注入，非检索式记忆）。"""

from .loader import load_memory_digest

__all__ = ["load_memory_digest"]
