"""mem0 记忆层实现：百炼 qwen3.5-omni-flash 抽取 + text-embedding-v3 向量 + 本地 Qdrant。

接线（ws_gateway/main，MEMORY_PROVIDER=mem0 时启用）：
- recall：pipeline 每轮拿 last_user_text 检索 → 结果进 system prompt
  记忆槽位（替代常驻 fixture 摘要）。检索失败静默降级 None——记忆是
  辅助，挂了不挡对话。
- record：轮末后台线程写回（mem0.add 内部打 LLM 做事实抽取，耗时秒级，
  必须离开 event loop）。
- seed_fixture：首次启动把 memory/*.json 蒸馏条目以 infer=False 直存
  （不走抽取，省 token），供检索命中。

Clash 注意：mem0 内部 openai client 走 httpx 默认代理发现，给 aliyuncs
域名补 NO_PROXY 直出。
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any

from .loader import MemoryPack, load_memory

#: 记忆检索阈值/top_k：宁缺毋滥——不贴切的记忆不进 prompt。
#: 阈值实测标定（text-embedding-v3 中文短文本地板分 ~0.5）：
#: "鸡鸣寺"→0.67 命中；"今天吃什么"/"我好困"→≤0.55 全砍。
DEFAULT_TOP_K = 5
DEFAULT_THRESHOLD = 0.6


class Mem0MemoryProvider:
    """MemoryProvider 的 mem0 实现；`client` 可注入假对象供测试。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        llm_model: str = "qwen3.5-omni-flash",
        embed_model: str = "text-embedding-v3",
        embed_dims: int = 1024,
        persist_dir: str | Path = "data/mem0",
        user_id: str = "local-user",
        top_k: int = DEFAULT_TOP_K,
        threshold: float = DEFAULT_THRESHOLD,
        client: Any = None,
    ) -> None:
        self.user_id = user_id
        self.top_k = top_k
        self.threshold = threshold
        self.persist_dir = Path(persist_dir)
        self._client = client or self._build(
            api_key,
            base_url,
            llm_model,
            embed_model,
            embed_dims,
            self.persist_dir / "qdrant",
        )

    @staticmethod
    def _build(
        api_key: str,
        base_url: str,
        llm_model: str,
        embed_model: str,
        embed_dims: int,
        store_path: Path,
    ) -> Any:
        # mem0 内部 OpenAI client 走 httpx 默认代理发现：aliyuncs 直出，
        # 绕开本机 Clash（与 providers/llm 的 trust_env=False 同一用意）。
        os.environ["NO_PROXY"] = ",".join(
            filter(
                None,
                [os.environ.get("NO_PROXY", ""), "aliyuncs.com,.aliyuncs.com"],
            )
        )
        # mem0 默认向 PostHog 发遥测——本地 PoC 关掉（也省一次外网往返）。
        os.environ.setdefault("MEM0_TELEMETRY_ENABLED", "false")
        from mem0 import Memory  # 可选依赖，懒导入

        return Memory.from_config(
            {
                "llm": {
                    "provider": "openai",
                    "config": {
                        "model": llm_model,
                        "api_key": api_key,
                        "openai_base_url": base_url,
                        "temperature": 0.1,
                    },
                },
                "embedder": {
                    "provider": "openai",
                    "config": {
                        "model": embed_model,
                        "api_key": api_key,
                        "openai_base_url": base_url,
                        "embedding_dims": embed_dims,
                    },
                },
                "vector_store": {
                    "provider": "qdrant",
                    "config": {
                        "collection_name": "paimon_memories",
                        "path": str(store_path.resolve()),
                        "embedding_model_dims": embed_dims,
                    },
                },
                "version": "v1.1",
            }
        )

    async def recall(self, query: str) -> str | None:
        q = (query or "").strip()
        if not q:
            return None
        try:
            res = await asyncio.to_thread(
                self._client.search,
                q,
                top_k=self.top_k,
                threshold=self.threshold,
                filters={"user_id": self.user_id},
            )
        except Exception:
            return None  # 检索失败不挡对话
        rows = res.get("results") if isinstance(res, dict) else res
        texts = [
            str(r.get("memory", "")).strip()
            for r in (rows or [])
            if isinstance(r, dict)
        ]
        return "；".join(t for t in texts if t) or None

    def record(self, user_text: str, assistant_text: str) -> None:
        """轮末写回——后台线程，mem0.add 的 LLM 抽取不堵 event loop。"""
        user_text = (user_text or "").strip()
        assistant_text = (assistant_text or "").strip()
        if not user_text or not assistant_text:
            return
        threading.Thread(
            target=self._safe_add,
            args=(user_text, assistant_text),
            daemon=True,
        ).start()

    def _safe_add(self, user_text: str, assistant_text: str) -> None:
        try:
            self._client.add(
                [
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": assistant_text},
                ],
                user_id=self.user_id,
            )
        except Exception:
            pass  # 记忆写失败静默——下次轮次再沉淀

    def seed_fixture(self, memory_dir: str | Path) -> int:
        """把 fixture 蒸馏条目直存进 mem0（infer=False 不走 LLM）。

        用 .seeded 标记文件保证只种一次；返回写入条数。
        """
        marker = self.persist_dir / ".seeded"
        if marker.exists():
            return 0
        pack: MemoryPack = load_memory(memory_dir)
        if not pack.text:
            return 0
        written = 0
        for chunk in pack.text.split("。"):
            chunk = chunk.strip("｜ ")
            if not chunk:
                continue
            try:
                self._client.add(
                    chunk, user_id=self.user_id, infer=False
                )
                written += 1
            except Exception:
                pass
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text("ok", encoding="utf-8")
        return written
