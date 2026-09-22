"""端到端语音 pipeline：Mic → VAD → Smart Turn → ASR → Core → LLM → Chunker → TTS → 播放。

组装形态（doc 02 §1 / 06 §1）：
- `audio` 任意 AsyncIterable[bytes]（MicCapture.frames() / 文件帧流），
  pump 每帧扇出三路：VAD analyze、SmartTurn append_audio、ASR 上行队列；
- VAD 迁移 → bus 发 USER_SPEECH_STARTED/STOPPED（打断链路与状态机只吃事件，
  本类不直接做轮次裁决——turn-taking C1/C3）；
- ASR partial/final → bus 事件 + 喂 SmartTurn 转写闸门（wait_for_transcript）；
- AGENT_CAN_RESPOND → 响应任务：LLM 结构化 token 流 → SpeechFieldExtractor
  增量抽 speech → ClauseChunker 语义边界 → TTS 流式合成 → 逐 chunk 播放。

投机执行（turn-taking C6 / low-latency C2）：
ASR partial 到达即用 partial 文本预构造 agent_input + messages 并发
PROMPT_PREBUILT；turn_complete 到达时文本一致 → speculative=hit 直接发
LLM 请求，不一致 → miss 重建，无 partial → none。投机结果可静默丢弃。

打断（turn-taking C4 / doc 03 §2.2）：六步执行在 InterruptionManager；
本类把在途响应 task 的 cancel 登记进 track_llm，被打断时整体取消，
不重复发 PLAYBACK_STOPPED（InterruptionManager 已发）。

延迟埋点（low-latency C5）：doc 06 §3 九时间戳全部经 bus 事件进入
LatencyLog；t_llm_request / prompt_prebuilt_at / speculative 为扩展口径。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterable, AsyncIterator, Callable
from typing import Any

from character.agent import CharacterAgent, is_noop
from conversation.core import ConversationCore
from conversation.events import Event, EventBus, EventType
from conversation.state_machine import ConversationState
from metrics.latency import LatencyLog
from providers.llm.base import parse_agent_reply

#: 结构化输出走 json_object 模式（与 CharacterAgent.respond 同一口径）。
_JSON_OBJECT_FORMAT = {"type": "json_object"}

#: heard-history 的 audio_s 估计：音频到达时按"最早未饱和 segment"分配，
#: 饱和阈值 = len(text) * 秒/字。中文 TTS 约 4–5 字/秒，取 0.22s/字做
#: 一阶近似；真实精度上限由 playback.position_seconds 兜底（context.py 注释）。
_SEC_PER_CHAR_ESTIMATE = 0.22

#: ClauseChunker 的语义边界字符（中英文标点 + 换行）。
_BOUNDARY_CHARS = frozenset("，。！？；：、…—,.!?;:\n")


class SpeechFieldExtractor:
    """从流式 JSON 输出中增量抽取 "speech" 字段的字符串值。

    不整段等 JSON 落地：识别到 `"speech": "` 后逐字符放出，遇到未转义的
    收尾引号结束；\\n/\\t/\\"/\\\\/\\uXXXX 等转义就地解码。LLM 把 speech
    放首字段时（我们的输出契约如此）TTS 能提前数百毫秒开工。
    """

    _KEY_RE = re.compile(r'"speech"\s*:\s*"')
    # key 前缀可能跨 token：正则最长前缀是 `"speech" : "`（10+字符），
    # 留 12 字符尾窗防止 key 被截断漏匹配。
    _KEEP_TAIL = 12
    _ESCAPES = {
        "n": "\n",
        "t": "\t",
        "r": "\r",
        "b": "\b",
        "f": "\f",
        '"': '"',
        "'": "'",
        "\\": "\\",
        "/": "/",
    }

    def __init__(self) -> None:
        self._buf = ""
        self._in_string = False
        self._done = False

    def feed(self, text: str) -> str:
        """喂一段 LLM token，返回新解码出的 speech 字符。"""
        if self._done:
            return ""
        self._buf += text
        out: list[str] = []
        if not self._in_string:
            m = self._KEY_RE.search(self._buf)
            if m is None:
                self._buf = self._buf[-self._KEEP_TAIL :]
                return ""
            self._buf = self._buf[m.end() :]
            self._in_string = True
        buf = self._buf
        i, n = 0, len(buf)
        while i < n:
            ch = buf[i]
            if ch == '"':
                self._done = True
                i += 1
                break
            if ch == "\\":
                if i + 1 >= n:
                    break  # 转义符被 token 边界切断，留到下轮
                esc = buf[i + 1]
                if esc == "u":
                    if i + 6 > n:
                        break  # \uXXXX 不完整，留到下轮
                    try:
                        out.append(chr(int(buf[i + 2 : i + 6], 16)))
                    except ValueError:
                        pass
                    i += 6
                    continue
                out.append(self._ESCAPES.get(esc, esc))
                i += 2
                continue
            out.append(ch)
            i += 1
        self._buf = buf[i:]
        return "".join(out)


class ClauseChunker:
    """Text Chunker（doc 02 §2）：把 speech 字符流按自然语义边界切成块。

    边界字符即切即放（标点随前块）；`flush()` 在 LLM 流结束时放出残余。
    不设最小长度——"哈？"这种两字块立刻发正是低延迟的意义。
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> list[str]:
        self._buf += text
        chunks: list[str] = []
        start = 0
        for i, ch in enumerate(self._buf):
            if ch in _BOUNDARY_CHARS:
                chunks.append(self._buf[start : i + 1])
                start = i + 1
        self._buf = self._buf[start:]
        return [c for c in chunks if c.strip()]

    def flush(self) -> list[str]:
        rest, self._buf = self._buf, ""
        return [rest] if rest.strip() else []


class VoicePipeline:
    """把采集/判定/识别/生成/合成/播放缝到一条事件总线上。

    依赖全部注入（真实 adapter 或 scripted fake 同形），本类只负责
    接线与时序；领域裁决全部在 ConversationCore。
    """

    def __init__(
        self,
        *,
        audio: AsyncIterable[bytes],
        vad: Any,
        turn: Any,
        asr: Any,
        agent: CharacterAgent,
        tts: Any,
        player: Any,
        core: ConversationCore | None = None,
        metrics: LatencyLog | None = None,
        asr_sample_rate: int = 16000,
        llm_params: dict[str, Any] | None = None,
        playback_drain_s: float = 30.0,
        auto_stop: bool = False,
        auto_stop_settle_s: float = 0.8,
        min_barge_in_s: float = 0.0,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._audio = audio
        self._vad = vad
        self._turn = turn
        self._asr = asr
        self._agent = agent
        self._tts = tts
        self._player = player
        self._asr_sample_rate = asr_sample_rate
        self._llm_params = dict(llm_params or {"max_tokens": 96})
        self._playback_drain_s = playback_drain_s
        self._now = now_fn

        self.core = core or ConversationCore(
            playback=player,
            tts=tts,
            min_barge_in_s=min_barge_in_s,
            now_fn=now_fn,
        )
        self.bus: EventBus = self.core.bus
        self.metrics = metrics or LatencyLog(self.bus, now_fn=now_fn)

        self._asr_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=256)
        self._tasks: list[asyncio.Task] = []
        self._respond_task: asyncio.Task | None = None
        self._prebuilt: dict[str, Any] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopped = asyncio.Event()
        self._audio_done = asyncio.Event()
        self._auto_stop = auto_stop
        self._auto_stop_settle_s = auto_stop_settle_s
        self.errors: list[str] = []

    # ---- 生命周期 ----

    async def run(self) -> None:
        """启动管线并阻塞到 stop()/音频源耗尽。须在 running loop 内调用。"""
        self._loop = asyncio.get_running_loop()
        await self._turn.setup()
        # Smart Turn 真触发的完成（含 transcript 闸门释放/静音兜底）走回调；
        # user_speech_stopped 返回的 incomplete verdict 只发 TURN_INCOMPLETE。
        self._turn.on_turn_complete = self._on_turn_verdict
        self.bus.subscribe(EventType.AGENT_CAN_RESPOND, self._on_can_respond)
        # partial 与 final 都触发预构造：final 常比最后一条 partial 更完整，
        # 在闸门放行（TURN_COMPLETE）前同步刷新 prebuilt，最大化 hit 率。
        self.bus.subscribe(EventType.ASR_PARTIAL, self._on_asr_partial)
        self.bus.subscribe(EventType.ASR_FINAL, self._on_asr_partial)
        self.bus.subscribe(
            EventType.USER_TURN_COMPLETE, self._on_turn_complete
        )
        self._tasks = [
            asyncio.create_task(self._audio_pump(), name="audio-pump"),
            asyncio.create_task(self._asr_loop(), name="asr-loop"),
            asyncio.create_task(self._tick_loop(), name="tick"),
        ]
        if self._auto_stop:
            self._tasks.append(
                asyncio.create_task(self._auto_stop_watch(), name="auto-stop")
            )
        try:
            await self._stopped.wait()
        finally:
            await self.shutdown()

    def stop(self) -> None:
        self._stopped.set()

    async def shutdown(self) -> None:
        """停采集侧、收尾响应任务、关闭 provider 与播放器、落盘 metrics。"""
        for task in self._tasks:
            task.cancel()
        if self._respond_task is not None and not self._respond_task.done():
            self._respond_task.cancel()
        await asyncio.gather(
            *self._tasks,
            *([self._respond_task] if self._respond_task else []),
            return_exceptions=True,
        )
        for closer in (
            getattr(self._asr, "close", None),
            getattr(self._turn, "cleanup", None),
            getattr(self._vad, "cleanup", None),
            getattr(self._tts, "close", None),
        ):
            if closer is not None:
                try:
                    await closer()
                except Exception:
                    pass
        close_player = getattr(self._player, "close", None)
        if close_player is not None:
            try:
                close_player()
            except Exception:
                pass
        self.metrics.close()

    # ---- 音频泵：一帧三路 ----

    async def _audio_pump(self) -> None:
        async for pcm in self._audio:
            if not pcm:
                continue
            res = await self._vad.analyze(pcm)
            if res.started:
                # 先发域事件：barge-in 的媒体层停播不应等 strategy 处理
                self.bus.publish(EventType.USER_SPEECH_STARTED)
                await self._turn.user_speech_started(
                    getattr(self._vad.params, "start_secs", 0.0)
                )
            if res.stopped:
                self.bus.publish(EventType.USER_SPEECH_STOPPED)
                verdict = await self._turn.user_speech_stopped(
                    getattr(self._vad.params, "stop_secs", 0.2)
                )
                if not verdict.complete:
                    # complete 的情况由 on_turn_complete 回调发 TURN_COMPLETE
                    self.bus.publish(
                        EventType.TURN_INCOMPLETE,
                        {
                            "probability": verdict.probability,
                            "inference_ms": verdict.inference_ms,
                        },
                    )
            await self._turn.append_audio(pcm)
            try:
                self._asr_queue.put_nowait(pcm)
            except asyncio.QueueFull:
                pass  # ASR 消费滞后时丢帧保实时（与 mic 队列同一取舍）
            # 让出调度：asr-loop/respond 与泵保持近似步进，避免快速音频源
            # （文件回放）把 ASR 事件甩到轮次裁决之后。
            await asyncio.sleep(0)
        # 音频源耗尽：冲刷尾部——用户还"说着"时文件/流结束，强制一次
        # VAD-stopped 语义让 Smart Turn 对缓存的语音出最终裁决。
        if self.core.turn_manager.turn_open and self.core.state in (
            ConversationState.LISTENING,
            ConversationState.POSSIBLE_END,
        ):
            self.bus.publish(EventType.USER_SPEECH_STOPPED)
            verdict = await self._turn.user_speech_stopped(
                getattr(self._vad.params, "stop_secs", 0.2)
            )
            if not verdict.complete:
                self.bus.publish(
                    EventType.TURN_INCOMPLETE,
                    {
                        "probability": verdict.probability,
                        "inference_ms": verdict.inference_ms,
                    },
                )
        self._asr_queue.put_nowait(None)
        self._audio_done.set()

    async def _asr_frames(self) -> AsyncIterator[bytes]:
        while True:
            item = await self._asr_queue.get()
            if item is None:
                return
            yield item

    async def _asr_loop(self) -> None:
        try:
            async for ev in self._asr.stream(
                self._asr_frames(), sample_rate=self._asr_sample_rate
            ):
                et = (
                    EventType.ASR_PARTIAL
                    if ev.kind == "partial"
                    else EventType.ASR_FINAL
                )
                # 先发域事件（TurnManager 更新 last_text），再喂转写闸门——
                # 闸门可能立刻触发轮次完成，保证裁决拿到的是最新文本
                self.bus.publish(et, {"text": ev.text, "raw": ev.raw})
                await self._turn.feed_transcript(
                    ev.text, finalized=ev.kind == "final"
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.errors.append(f"asr: {e}")
            self.bus.publish(EventType.PIPELINE_ERROR, {"stage": "asr", "error": str(e)})

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            self.core.tick()

    def _on_turn_verdict(self, verdict: Any) -> None:
        """SmartTurnAdapter 完成回调 → 域事件 TURN_COMPLETE。"""
        self.bus.publish(
            EventType.TURN_COMPLETE,
            {
                "source": verdict.source,
                "probability": verdict.probability,
                "inference_ms": verdict.inference_ms,
            },
        )

    async def _auto_stop_watch(self) -> None:
        """--file/--mock 用：音频源与 ASR 收尾后、无在途轮次/响应即整体停。"""
        await self._audio_done.wait()
        while True:
            await asyncio.sleep(self._auto_stop_settle_s)
            if self._idle():
                self.stop()
                return

    def _idle(self) -> bool:
        # 音频源与 ASR 流都已收尾后，未关闭的轮次不可能再被裁决
        # （无新音频喂 VAD/Smart Turn，无新转写放行闸门）——respond
        # 任务结清即视为 idle，不管 TurnManager.turn_open。
        asr_done = self._tasks[1].done()  # asr-loop
        respond_done = (
            self._respond_task is None or self._respond_task.done()
        )
        return asr_done and respond_done

    # ---- 投机预构造（C6） ----

    def _on_asr_partial(self, event: Event) -> None:
        """ASR partial 到达即预构造 prompt；turn_complete 时可瞬时发 LLM。"""
        if self.core.state == ConversationState.SILENCED:
            return
        tm = self.core.turn_manager
        if not tm.turn_open:
            return
        text = event.payload.get("text") or ""
        if not text or (
            self._prebuilt is not None and self._prebuilt["text"] == text
        ):
            return
        agent_input = self.core.context.build_agent_input(
            state=ConversationState.THINKING,
            last_user_text=text,
            consume_interruption=False,  # 预构造不消费 interruption context
        )
        self._prebuilt = {
            "turn_id": tm.turn_id,
            "text": text,
            "agent_input": agent_input,
            "messages": self._agent.build_messages(agent_input),
        }
        self.bus.publish(
            EventType.PROMPT_PREBUILT, {"turn_id": tm.turn_id, "text": text}
        )

    def _on_turn_complete(self, event: Event) -> None:
        self.core.context.record_user_turn(
            event.payload.get("text", ""),
            turn_id=event.payload.get("turn_id"),
        )

    # ---- 响应任务：LLM → chunker → TTS → 播放 ----

    def _on_can_respond(self, event: Event) -> None:
        assert self._loop is not None
        if self._respond_task is not None and not self._respond_task.done():
            self._respond_task.cancel()  # 新轮次取代在途响应（防御）
        self._respond_task = self._loop.create_task(
            self._respond(
                int(event.payload.get("turn_id") or 0),
                event.payload.get("text", ""),
            )
        )

    def _resolve_prompt(
        self, turn_id: int, text: str
    ) -> tuple[dict[str, Any], list, str]:
        """命中预构造则复用（hit），否则按 final 文本重建（miss/none）。"""
        prebuilt = self._prebuilt
        self._prebuilt = None
        if (
            prebuilt is not None
            and prebuilt["turn_id"] == turn_id
            and prebuilt["text"] == text
        ):
            # 预构造 peek 了 interruption context 但未消费，这里正式消费
            self.core.context.pending_interruption = None
            return prebuilt["agent_input"], prebuilt["messages"], "hit"
        agent_input = self.core.context.build_agent_input(
            state=ConversationState.THINKING, last_user_text=text
        )
        messages = self._agent.build_messages(agent_input)
        return agent_input, messages, ("miss" if prebuilt else "none")

    async def _respond(self, turn_id: int, text: str) -> None:
        ctx = self.core.context
        _, messages, spec = self._resolve_prompt(turn_id, text)
        utterance = ctx.begin_utterance()
        self.bus.publish(
            EventType.LLM_STARTED, {"turn_id": turn_id, "speculative": spec}
        )
        task = asyncio.current_task()
        if task is not None:
            self.core.interruption.track_llm(task.cancel)
        chunk_q: asyncio.Queue[str | None] = asyncio.Queue()
        audio_task: asyncio.Task | None = None
        got_token = False
        try:
            extractor = SpeechFieldExtractor()
            chunker = ClauseChunker()
            raw_parts: list[str] = []

            async def produce() -> None:
                nonlocal got_token
                async for token in self._agent.stream_reply(
                    messages,
                    response_format=_JSON_OBJECT_FORMAT,
                    **self._llm_params,
                ):
                    if not got_token:
                        got_token = True
                        self.bus.publish(
                            EventType.LLM_TOKEN, {"turn_id": turn_id, "first": True}
                        )
                    raw_parts.append(token)
                    piece = extractor.feed(token)
                    if piece:
                        utterance.add_generated(piece)
                        for clause in chunker.feed(piece):
                            chunk_q.put_nowait(clause)
                for clause in chunker.flush():
                    utterance.add_generated(clause)
                    chunk_q.put_nowait(clause)

            async def audio_out() -> None:
                first = await chunk_q.get()
                if first is None:
                    return  # NOOP：整轮没有可合成的文本
                self.bus.publish(
                    EventType.TTS_STARTED, {"turn_id": turn_id}
                )

                async def chunks() -> AsyncIterator[str]:
                    # 文本发出去的时刻即"已交付 TTS"——同步记 segment
                    utterance.add_segment(first, 0.0)
                    yield first
                    while True:
                        item = await chunk_q.get()
                        if item is None:
                            return
                        utterance.add_segment(item, 0.0)
                        yield item

                first_audio = True
                async for pcm in self._tts.stream_audio(chunks()):
                    seg = self._audio_segment(utterance)
                    seg.audio_s += len(pcm) / self._bytes_per_sec
                    if first_audio:
                        first_audio = False
                        self.bus.publish(
                            EventType.FIRST_AUDIO, {"turn_id": turn_id}
                        )
                    self._player.write(pcm)
                    self.bus.publish(
                        EventType.AGENT_SPEAKING, {"turn_id": turn_id}
                    )

            producer = asyncio.create_task(produce())
            audio_task = asyncio.create_task(audio_out())
            try:
                await producer
            finally:
                chunk_q.put_nowait(None)
            await audio_task
            audio_task = None

            reply = parse_agent_reply("".join(raw_parts))
            noop = is_noop(reply)
            self.bus.publish(
                EventType.AGENT_REPLY,
                {
                    "turn_id": turn_id,
                    "speech": reply.speech,
                    "emotion": reply.emotion,
                    "energy": reply.energy,
                    "should_continue": reply.should_continue,
                    "noop": noop,
                },
            )
            await self._finish_playback(
                utterance, reason="noop" if noop else "completed"
            )
        except asyncio.CancelledError:
            # barge-in：InterruptionManager 已发 AGENT_INTERRUPTED +
            # PLAYBACK_STOPPED 并封账 heard history，这里只清理
            raise
        except Exception as e:
            self.errors.append(f"respond: {e}")
            self.bus.publish(
                EventType.AGENT_REPLY,
                {"turn_id": turn_id, "speech": "", "error": str(e)},
            )
            await self._finish_playback(utterance, reason="error")
        finally:
            if task is not None:
                self.core.interruption.untrack_llm(task.cancel)
            if audio_task is not None and not audio_task.done():
                audio_task.cancel()
                await asyncio.gather(audio_task, return_exceptions=True)

    async def _finish_playback(self, utterance, *, reason: str) -> None:
        """等播放 buffer 排空后发 PLAYBACK_STOPPED 封账本轮。"""
        if not utterance.open:
            return  # 已被打断封账
        deadline = self._now() + self._playback_drain_s
        pending = getattr(self._player, "pending_seconds", None)
        if pending is not None:
            while self._player.pending_seconds > 0 and self._now() < deadline:
                await asyncio.sleep(0.02)
        played = 0.0
        stop = getattr(self._player, "stop", None)
        if stop is not None:
            played = stop() or 0.0
        if not utterance.open:
            return  # 排空期间被打断
        self.bus.publish(
            EventType.PLAYBACK_STOPPED,
            {"reason": reason, "played_s": played},
        )

    def _audio_segment(self, utterance):
        """音频时长分配到"最早未饱和"的 text segment（一阶近似）。"""
        segs = utterance.segments
        if not segs:
            utterance.add_segment("", 0.0)
            return utterance.segments[-1]
        for seg in segs:
            if seg.audio_s < len(seg.text) * _SEC_PER_CHAR_ESTIMATE:
                return seg
        return segs[-1]

    @property
    def _bytes_per_sec(self) -> float:
        rate = getattr(self._tts, "sample_rate", 24000)
        return float(rate) * 2  # int16 mono
