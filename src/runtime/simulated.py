"""脚本化 provider 与音频源：无网络/无声卡环境下端到端驱动 pipeline。

用途：
- tests/test_pipeline.py：全脚本化确定性驱动（speculative hit/miss、
  barge-in、NOOP、九时间戳断言）；
- `python -m runtime.main --mock --file …`：真实 VAD + Smart Turn +
  脚本化 ASR/LLM/TTS（本环境无有效 API key，见 TASK-003 阻塞记录）。

这里的类刻意保持与真实 adapter 同形（duck-typed），pipeline 不区分真假：
- ScriptedVAD ≈ SileroVADAdapter（analyze → started/stopped/user_speaking）
- ScriptedTurn ≈ SmartTurnAdapter（on_turn_complete 回调 + verdict 返回）
- ScriptedASR ≈ DashScopeASR（能量门控出 partial/final ASREvent）
- ScriptedLLM ≈ OpenAICompatibleLLM（按 token 粒度吐 JSON）
- ToneTTS ≈ FishAudioTTS（语义块 → 正弦 PCM，cancel 三契约）
- WavSinkPlayer ≈ StreamingPlayer（write/stop/position_seconds 契约）
"""

from __future__ import annotations

import asyncio
import json
import math
import struct
import time
import wave
from collections.abc import AsyncIterable, AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from providers.asr.base import ASREvent, ASRProvider
from providers.llm.base import AgentReply, LLMProvider, parse_agent_reply
from providers.tts.base import TTSProvider

#: 与 MicCapture/Silero 约定一致的帧粒度：16kHz int16 mono 32ms = 1024B。
FRAME_BYTES_16K_32MS = 1024


# ---------------------------------------------------------------- audio 源


async def pcm_file_frames(
    path: str | Path,
    *,
    frame_bytes: int = FRAME_BYTES_16K_32MS,
    realtime: bool = True,
) -> AsyncIterator[bytes]:
    """把 int16 PCM 裸流文件切成帧异步吐出；realtime 按帧时长定速。"""
    frame_s = frame_bytes / (16000 * 2)
    with Path(path).open("rb") as f:
        while True:
            chunk = f.read(frame_bytes)
            if not chunk:
                return
            yield chunk
            if realtime:
                await asyncio.sleep(frame_s)


async def list_frames(frames: list[bytes]) -> AsyncIterator[bytes]:
    """测试用：内存帧序列做音频源。"""
    for f in frames:
        yield f


def silence_frame(n_bytes: int = FRAME_BYTES_16K_32MS) -> bytes:
    return b"\x00" * n_bytes


def tone_frame(
    n_bytes: int = FRAME_BYTES_16K_32MS, *, freq: float = 220.0, amp: int = 9000
) -> bytes:
    """生成一段有能量的 PCM（给能量门控 ScriptedASR 判 speech 用）。"""
    n = n_bytes // 2
    samples = [
        int(amp * math.sin(2 * math.pi * freq * i / 16000)) for i in range(n)
    ]
    return struct.pack(f"<{n}h", *samples)


# ---------------------------------------------------------------- VAD / Turn


class ScriptedVAD:
    """按帧序号报告 speech 区间 [start, end)；duck-type SileroVADAdapter。"""

    def __init__(self, segments: list[tuple[int, int]]) -> None:
        # segments: [(start_frame, end_frame), ...]，帧号从 0 计
        self._segments = sorted(segments)
        self._frame = 0
        self._speaking = False
        self.params = SimpleNamespace(start_secs=0.032, stop_secs=0.2)

    async def analyze(self, pcm: bytes) -> SimpleNamespace:
        in_speech = any(s <= self._frame < e for s, e in self._segments)
        started = in_speech and not self._speaking
        stopped = not in_speech and self._speaking
        self._speaking = in_speech
        self._frame += 1
        return SimpleNamespace(
            state="SPEAKING" if in_speech else "QUIET",
            user_speaking=self._speaking,
            started=started,
            stopped=stopped,
        )

    async def cleanup(self) -> None:
        pass


class ScriptedTurn:
    """duck-type SmartTurnAdapter：停顿即判 complete（可配延迟/先判 incomplete）。

    - `complete_delay_s > 0`：user_speech_stopped 返回 INCOMPLETE verdict，
      延迟后经 on_turn_complete 回调补发 COMPLETE（模拟转写闸门放行）。
    - `verdicts` 可给定一串 bool 消费（False → INCOMPLETE 不回调）。
    """

    def __init__(
        self,
        *,
        complete_delay_s: float = 0.0,
        verdicts: list[bool] | None = None,
        release_on_final: bool = False,
        source: str = "model",
    ) -> None:
        self.params = SimpleNamespace(
            stop_secs=1.2, pre_speech_ms=0, max_duration_secs=8
        )
        self.on_turn_complete: Callable[[Any], None] | None = None
        self._delay = complete_delay_s
        self._verdicts = list(verdicts) if verdicts else None
        # release_on_final：模拟真实 strategy 的 wait_for_transcript 闸门——
        # user_speech_stopped 返回 INCOMPLETE，finalized 转写到达才放行。
        self._release_on_final = release_on_final
        self._awaiting_release = False
        self._source = source
        self.transcripts: list[tuple[str, bool]] = []  # feed_transcript 记录

    async def setup(self) -> None:
        pass

    async def append_audio(self, pcm: bytes) -> None:
        pass

    async def user_speech_started(self, start_secs: float = 0.0) -> None:
        pass

    def _verdict(self, complete: bool) -> Any:
        return SimpleNamespace(
            complete=complete,
            probability=0.9 if complete else 0.2,
            inference_ms=10.0 if complete else 5.0,
            source=self._source if complete else None,
        )

    async def user_speech_stopped(self, stop_secs: float = 0.0) -> Any:
        complete = self._verdicts.pop(0) if self._verdicts else True
        if not complete:
            return self._verdict(False)
        if self._release_on_final:
            # 判 complete 但等 finalized 转写放行（真实 wait_for_transcript 语义）
            self._awaiting_release = True
            return self._verdict(False)
        if self._delay > 0:

            async def _deferred() -> None:
                await asyncio.sleep(self._delay)
                if self.on_turn_complete:
                    self.on_turn_complete(self._verdict(True))

            asyncio.create_task(_deferred())
            return self._verdict(False)
        if self.on_turn_complete:
            self.on_turn_complete(self._verdict(True))
        return self._verdict(True)

    async def feed_transcript(self, text: str, *, finalized: bool) -> None:
        self.transcripts.append((text, finalized))
        if finalized and self._release_on_final and self._awaiting_release:
            self._awaiting_release = False
            if self.on_turn_complete:
                self.on_turn_complete(self._verdict(True))

    async def clear(self) -> None:
        pass

    async def cleanup(self) -> None:
        pass


# ---------------------------------------------------------------- ASR


class ScriptedASR(ASRProvider):
    """能量门控脚本 ASR：PCM RMS 超阈值视为 speech，推进式吐 partial，
    speech 区域结束（连续静音）后吐一次 final，进入下一句转写。

    transcripts 按 speech 区域顺序消费；不够用时重复最后一句。
    与真实 ASR 一样：partial 是"同一句话的累计前缀"，final 是整句。
    """

    def __init__(
        self,
        transcripts: str | list[str],
        *,
        rms_threshold: float = 300.0,
        silence_frames_end: int = 6,
        partial_every: int = 4,
        chars_per_partial: int = 3,
    ) -> None:
        if isinstance(transcripts, str):
            transcripts = [transcripts]
        self._transcripts = list(transcripts)
        self._rms_threshold = rms_threshold
        self._silence_end = silence_frames_end
        self._partial_every = partial_every
        self._chars_per_partial = chars_per_partial

    async def stream(
        self,
        audio: AsyncIterable[bytes],
        *,
        sample_rate: int = 16000,
        **kwargs: Any,
    ) -> AsyncIterator[ASREvent]:
        idx = 0
        in_speech = False
        speech_frames = 0
        quiet_run = 0
        last_partial = ""

        def transcript() -> str:
            return self._transcripts[min(idx, len(self._transcripts) - 1)]

        async for pcm in audio:
            rms = _rms(pcm)
            if rms >= self._rms_threshold:
                in_speech = True
                speech_frames += 1
                quiet_run = 0
                if speech_frames % self._partial_every == 0:
                    text = transcript()[
                        : min(
                            len(transcript()),
                            self._chars_per_partial
                            * (speech_frames // self._partial_every),
                        )
                    ]
                    if text and text != last_partial:
                        last_partial = text
                        yield ASREvent("partial", text)
            elif in_speech:
                quiet_run += 1
                if quiet_run >= self._silence_end:
                    yield ASREvent("final", transcript())
                    idx += 1
                    in_speech = False
                    speech_frames = 0
                    quiet_run = 0
                    last_partial = ""
        if in_speech:
            yield ASREvent("final", transcript())

    async def close(self) -> None:
        pass


def _rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    return math.sqrt(sum(s * s for s in samples) / n)


# ---------------------------------------------------------------- LLM


class ScriptedLLM(LLMProvider):
    """脚本化 LLM：把固定 AgentReply JSON 按 token_size 粒度流式吐出。

    `first_token_delay_s` 模拟 TTFT；`requests` 记录每次 stream_reply 的
    messages（测试断言投机 hit 时提交的正是预构造的 messages）。
    `reply` 可为 dict（自动 json.dumps）、str（原样流）或 callable
    （messages → reply，支持按输入变化）。
    """

    def __init__(
        self,
        reply: dict[str, Any] | str | Callable[[Any], dict[str, Any]],
        *,
        token_size: int = 4,
        token_delay_s: float = 0.0,
        first_token_delay_s: float = 0.0,
    ) -> None:
        self._reply = reply
        self._token_size = token_size
        self._token_delay = token_delay_s
        self._first_delay = first_token_delay_s
        self.requests: list[Any] = []

    def _render(self, messages: Any) -> str:
        reply = self._reply(messages) if callable(self._reply) else self._reply
        if isinstance(reply, str):
            return reply
        return json.dumps(reply, ensure_ascii=False)

    async def stream_reply(
        self, messages: Any, **kwargs: Any
    ) -> AsyncIterator[str]:
        self.requests.append(messages)
        if self._first_delay:
            await asyncio.sleep(self._first_delay)
        text = self._render(messages)
        for i in range(0, len(text), self._token_size):
            yield text[i : i + self._token_size]
            if self._token_delay:
                await asyncio.sleep(self._token_delay)

    async def complete_structured(
        self, messages: Any, **kwargs: Any
    ) -> AgentReply:
        self.requests.append(messages)
        return parse_agent_reply(self._render(messages))

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------- TTS


class ToneTTS(TTSProvider):
    """正弦波假 TTS：每个文本块合成 secs_per_chunk 秒 PCM，按 30ms 帧吐。

    cancel 三契约（与真实 adapter 同口径）：
    1. cancel() 后迭代立刻不再产音频；
    2. 本地无缓冲残留（本来就不攒 buffer）；
    3. cancel 后再调 stream_audio 是干净的新合成。
    """

    def __init__(
        self,
        *,
        sample_rate: int = 24000,
        secs_per_chunk: float = 0.15,
        chunk_frames: int = 5,
        frame_delay_s: float = 0.0,
    ) -> None:
        self.sample_rate = sample_rate
        self._secs_per_chunk = secs_per_chunk
        self._chunk_frames = chunk_frames
        self._frame_delay = frame_delay_s
        self._cancelled = False
        self.synthesized: list[str] = []
        self.cancelled_count = 0

    async def stream_audio(
        self, chunks: AsyncIterable[str], **kwargs: Any
    ) -> AsyncIterator[bytes]:
        self._cancelled = False  # cancel 契约 3：重开即干净的新合成
        frame_n = self._chunk_frames
        async for text in chunks:
            if self._cancelled:
                return
            self.synthesized.append(text)
            total = int(self.sample_rate * self._secs_per_chunk)
            per = total // frame_n
            for i in range(frame_n):
                if self._cancelled:
                    return
                yield _tone_pcm(per, self.sample_rate, freq=440 + len(text) * 20)
                if self._frame_delay:
                    await asyncio.sleep(self._frame_delay)

    async def cancel(self) -> None:
        self._cancelled = True
        self.cancelled_count += 1

    async def close(self) -> None:
        pass


def _tone_pcm(n_samples: int, sample_rate: int, *, freq: float = 440.0) -> bytes:
    samples = [
        int(6000 * math.sin(2 * math.pi * freq * i / sample_rate))
        for i in range(n_samples)
    ]
    return struct.pack(f"<{n_samples}h", *samples)


# ---------------------------------------------------------------- 播放落点


class WavSinkPlayer:
    """无输出设备的播放落点：write 累积 PCM，按"实时播放速度"折算已播位置。

    契约对齐 StreamingPlayer：
    - position_seconds：已"播出"秒数（realtime=True 时 = min(已写入时长,
      距首次写入的墙钟)，模拟扬声器按真实速率消费）；
    - pending_seconds：已写未播；
    - stop()：清未播、返回已播秒数（barge-in 的 heard 边界）；
    - close(path)：把全部写入落盘 wav（人工验收可听/可看波形）。
    """

    def __init__(
        self,
        *,
        sample_rate: int = 24000,
        channels: int = 1,
        out_path: str | Path | None = None,
        realtime: bool = True,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sample_rate = sample_rate
        self._bytes_per_sec = sample_rate * channels * 2
        self._out_path = Path(out_path) if out_path else None
        self._realtime = realtime
        self._now = now_fn
        self._chunks: list[bytes] = []
        self._written = 0
        self._t0: float | None = None
        self._playing = False

    @property
    def written_seconds(self) -> float:
        return self._written / self._bytes_per_sec

    @property
    def position_seconds(self) -> float:
        if not self._realtime:
            return self.written_seconds
        if self._t0 is None or not self._playing:
            return 0.0
        return min(self.written_seconds, self._now() - self._t0)

    @property
    def pending_seconds(self) -> float:
        return max(0.0, self.written_seconds - self.position_seconds)

    @property
    def is_playing(self) -> bool:
        return self._playing

    def write(self, pcm: bytes) -> None:
        if not pcm:
            return
        self._chunks.append(bytes(pcm))
        self._written += len(pcm)
        if self._t0 is None:
            self._t0 = self._now()
        self._playing = True

    def stop(self) -> float:
        played = self.position_seconds
        self._playing = False
        self._t0 = None
        return played

    def close(self) -> None:
        if self._out_path is not None and self._chunks:
            self._out_path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(self._out_path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(self._sample_rate)
                w.writeframes(b"".join(self._chunks))
        self._chunks.clear()
