"""TASK-002 acceptance 1：三个 Provider 抽象类存在，业务层不直接 import 供应商 SDK。

verify_by: grep 确认 src/ 内除 providers/ 外无供应商 SDK import；
pytest tests/test_providers.py 通过。
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path

import pytest

from providers.asr import ASREvent, ASRProvider
from providers.llm import AgentReply, LLMProvider
from providers.tts import TTSProvider

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"

# 供应商 SDK 清单；adapter 目录（providers/、turn/）以外禁止 import。
# pipecat 是 pipeline 底盘（D-002）不是供应商 SDK，不在此列。
VENDOR_SDKS = {
    "dashscope",
    "openai",
    "fish_audio_sdk",
    "fishaudio",
    "modelscope",
    "funasr",
    "elevenlabs",
    "minimax",
    "pyaudio",
}
ADAPTER_DIRS = {"providers", "turn"}


def test_abstract_providers_exist():
    assert inspect.isabstract(ASRProvider)
    assert inspect.isabstract(LLMProvider)
    assert inspect.isabstract(TTSProvider)
    for cls in (ASRProvider, LLMProvider, TTSProvider):
        with pytest.raises(TypeError):
            cls()  # type: ignore[abstract]


def test_asr_event_surface_only_partial_final():
    """streaming-asr-zh C3：ASR 事件面只含 partial/final，不携带轮次语义。"""
    partial = ASREvent(kind="partial", text="我觉得这个")
    final = ASREvent(kind="final", text="我觉得这个比赛吧")
    assert partial.kind == "partial"
    assert final.kind == "final"
    assert not hasattr(partial, "is_turn_complete")
    with pytest.raises((TypeError, ValueError)):
        ASREvent(kind="turn_complete", text="x")  # type: ignore[arg-type]


async def test_minimal_fake_providers_satisfy_contract():
    """最小 fake 实现能被业务层以抽象类型消费。"""

    class FakeASR(ASRProvider):
        async def stream(
            self, audio: AsyncIterable[bytes], **kw
        ) -> AsyncIterator[ASREvent]:
            async for _ in audio:
                yield ASREvent(kind="partial", text="嗯")
            yield ASREvent(kind="final", text="嗯，我想想")

        async def close(self) -> None:
            pass

    async def frames() -> AsyncIterator[bytes]:
        yield b"\x00" * 320

    events = [e async for e in FakeASR().stream(frames())]
    assert [e.kind for e in events] == ["partial", "final"]

    class FakeTTS(TTSProvider):
        def __init__(self) -> None:
            self.cancelled = False
            self.buffer: list[bytes] = []

        async def stream_audio(
            self, chunks: AsyncIterable[str]
        ) -> AsyncIterator[bytes]:
            async for _text in chunks:
                if self.cancelled:
                    return
                audio = b"audio"
                self.buffer.append(audio)
                yield audio

        async def cancel(self) -> None:
            # 契约：停推 + 清 buffer
            self.cancelled = True
            self.buffer.clear()

    async def texts() -> AsyncIterator[str]:
        yield "哈？"

    tts = FakeTTS()
    chunks = [c async for c in tts.stream_audio(texts())]
    assert chunks == [b"audio"]
    await tts.cancel()
    assert tts.buffer == []


def test_no_vendor_sdk_imports_outside_adapters():
    """src/ 下除 providers/ 与 turn/ 外，禁止 import 供应商 SDK。"""
    violations: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        rel = path.relative_to(SRC_ROOT)
        if rel.parts[0] in ADAPTER_DIRS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                root = name.split(".")[0]
                if root in VENDOR_SDKS:
                    violations.append(f"{rel}: imports {name}")
    assert violations == [], "\n".join(violations)


def test_agent_reply_shape():
    reply = AgentReply(speech="哈？你认真的？", emotion="teasing", energy=0.8)
    assert reply.should_continue is False
