"""TASK-002 acceptance 2：LLM adapter 对 mock OpenAI-compatible endpoint
流式返回 token，并能解析结构化输出。

verify_by: pytest tests/test_llm_adapter.py 通过。
"""

from __future__ import annotations

import json

import httpx
import pytest

from providers.llm import (
    AgentReply,
    LLMError,
    OpenAICompatibleLLM,
    StructuredOutputError,
    parse_agent_reply,
)

BASE_URL = "http://mock-llm/v1"


def sse_body(tokens: list[str]) -> bytes:
    lines = []
    for token in tokens:
        chunk = {"choices": [{"delta": {"content": token}}]}
        lines.append(f"data: {json.dumps(chunk, ensure_ascii=False)}")
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


def make_llm(handler) -> OpenAICompatibleLLM:
    return OpenAICompatibleLLM(
        base_url=BASE_URL,
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )


MESSAGES = [{"role": "user", "content": "你好"}]


async def test_streams_tokens_from_sse():
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=sse_body(["哈？", "真的", "假的？"]))

    llm = make_llm(handler)
    tokens = [t async for t in llm.stream_reply(MESSAGES)]

    assert tokens == ["哈？", "真的", "假的？"]
    assert "".join(tokens) == "哈？真的假的？"

    request = seen_requests[0]
    assert request.url == f"{BASE_URL}/chat/completions"
    assert request.headers["authorization"] == "Bearer test-key"
    body = json.loads(request.content)
    assert body["stream"] is True
    assert body["model"] == "test-model"
    assert body["messages"] == MESSAGES
    await llm.close()


async def test_complete_structured_parses_agent_reply():
    reply_json = json.dumps(
        {
            "speech": "哈？你认真的？",
            "emotion": "teasing",
            "energy": 0.8,
            "should_continue": False,
        },
        ensure_ascii=False,
    )
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        # 模拟模型把 JSON 拆成多段 token 流式吐出来
        return httpx.Response(
            200, content=sse_body([reply_json[:12], reply_json[12:40], reply_json[40:]])
        )

    llm = make_llm(handler)
    reply = await llm.complete_structured(MESSAGES)

    assert reply == AgentReply(
        speech="哈？你认真的？", emotion="teasing", energy=0.8, should_continue=False
    )
    assert seen[0]["response_format"] == {"type": "json_object"}
    await llm.close()


async def test_http_error_raises_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    llm = make_llm(handler)
    with pytest.raises(LLMError, match="HTTP 500"):
        _ = [t async for t in llm.stream_reply(MESSAGES)]
    await llm.close()


def test_parse_agent_reply_tolerates_fence_and_defaults():
    fenced = '前缀\n```json\n{"speech": "哦。", "emotion": "curious"}\n```\n后缀'
    reply = parse_agent_reply(fenced)
    # 未知 emotion 按 chinese-tts-eval C5 降级 neutral
    assert reply == AgentReply(speech="哦。", emotion="neutral", energy=0.5)

    clamped = parse_agent_reply('{"speech": "x", "energy": 9}')
    assert clamped.energy == 1.0

    string_fallback = parse_agent_reply('"嘿，还在发呆吗？"')
    assert string_fallback == AgentReply(speech="嘿，还在发呆吗？")
    assert parse_agent_reply('["嘿", "！"]').speech == "嘿！"
    assert parse_agent_reply("[30]").speech == ""

    with pytest.raises(StructuredOutputError):
        parse_agent_reply("完全不是 JSON")
