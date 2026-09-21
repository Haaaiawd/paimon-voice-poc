"""OpenAI-compatible LLM adapter（D-004：赛马统一接口，TTFT 优先）。

覆盖 DeepSeek / 通义千问 compatible-mode 等 `/chat/completions` 端点。
用 httpx SSE 流式读取，不依赖 openai SDK——供应商差异只体现在
base_url / api_key / model 三个构造参数上。

网络注意：本机 Clash 代理会给每连接 +~1.9s TLS 开销（见 .env.example），
默认 trust_env=False 直连；需要走代理时显式传 trust_env=True。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import httpx

from .base import (
    AgentReply,
    ChatMessage,
    LLMError,
    LLMProvider,
    parse_agent_reply,
)

_JSON_OBJECT_FORMAT: Mapping[str, Any] = {"type": "json_object"}


class OpenAICompatibleLLM(LLMProvider):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        trust_env: bool = False,
        default_params: Mapping[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.default_params = dict(default_params or {})
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            trust_env=trust_env,
            transport=transport,
        )

    async def _request_lines(
        self, payload: Mapping[str, Any]
    ) -> AsyncIterator[str]:
        try:
            async with self._client.stream(
                "POST", f"{self.base_url}/chat/completions", json=payload
            ) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    raise LLMError(
                        f"chat/completions HTTP {resp.status_code}: {resp.text[:500]}"
                    )
                async for line in resp.aiter_lines():
                    yield line
        except httpx.HTTPError as e:
            raise LLMError(f"chat/completions request failed: {e}") from e

    async def stream_reply(
        self,
        messages: Sequence[ChatMessage],
        *,
        response_format: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "stream": True,
            **self.default_params,
            **kwargs,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        async for line in self._request_lines(payload):
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                return
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            for choice in chunk.get("choices") or ():
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if content:
                    yield content

    async def complete_structured(
        self,
        messages: Sequence[ChatMessage],
        **kwargs: Any,
    ) -> AgentReply:
        parts = [
            token
            async for token in self.stream_reply(
                messages, response_format=_JSON_OBJECT_FORMAT, **kwargs
            )
        ]
        return parse_agent_reply("".join(parts))

    async def close(self) -> None:
        await self._client.aclose()
