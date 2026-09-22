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
import json
import re
import time
from collections.abc import AsyncIterable, AsyncIterator, Callable
from difflib import SequenceMatcher
from typing import Any

from character.agent import CharacterAgent, is_noop
from conversation.core import ConversationCore
from conversation.events import (
    Event,
    EventBus,
    EventType,
    TEXT_TURN_SOURCE,
)
from conversation.state_machine import ConversationState
from metrics.latency import LatencyLog
from providers.llm.base import StructuredOutputError, parse_agent_reply

#: 结构化输出走 json_object 模式（与 CharacterAgent.respond 同一口径）。
_JSON_OBJECT_FORMAT = {"type": "json_object"}


def _looks_like_contract(text: str) -> bool:
    """输出是否符合契约形：dict 含 speech 键，或字符串/字符串数组降级
    （parse_agent_reply 的合法降级面）。数字/数字数组这类垃圾 → False。"""
    try:
        parsed = json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        return '"speech"' in text  # 坏 JSON 但带字段 → 打捞路径已兜过
    if isinstance(parsed, dict):
        return "speech" in parsed
    if isinstance(parsed, str):
        return True
    if isinstance(parsed, list):
        return bool(parsed) and all(isinstance(x, str) for x in parsed)
    return False

#: heard-history 的 audio_s 估计：音频到达时按"最早未饱和 segment"分配，
#: 饱和阈值 = len(text) * 秒/字。中文 TTS 约 4–5 字/秒，取 0.22s/字做
#: 一阶近似；真实精度上限由 playback.position_seconds 兜底（context.py 注释）。
_SEC_PER_CHAR_ESTIMATE = 0.22

#: ClauseChunker 的语义边界字符（中英文标点 + 换行）。
_BOUNDARY_CHARS = frozenset("，。！？；：、…—,.!?;:\n")

#: 自听回声判定（软 AEC）：扬声器外放时麦克风收回派蒙自己的声音，
#: ASR 转写与最近 assistant 播出文本高度重合 → 整轮丢弃。
_ECHO_SIMILARITY = 0.6
_ECHO_MIN_CHARS = 4  # 短于此不判定（"嗯/好"巧合率太高）
_ECHO_LOOKBACK = 3  # 只比对最近 N 条 utterance（回声是即时的）


def _norm_speech(text: str) -> str:
    """回声比对归一化：只留字母/数字/汉字，忽略标点、空白、大小写。"""
    return "".join(ch for ch in text.lower() if ch.isalnum())


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
        initiative_config: Any = None,
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
            initiative_config=initiative_config,
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
        self.bus.subscribe(
            EventType.INITIATIVE_TRIGGERED, self._on_initiative
        )
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

    async def _asr_frames(self, first: bytes) -> AsyncIterator[bytes]:
        yield first
        while True:
            item = await self._asr_queue.get()
            if item is None:
                return
            yield item

    async def _asr_loop(self) -> None:
        """懒连接 + 断线重连：等首帧 PCM 进队才开 ASR 流（空闲期不占
        socket，规避 DashScope WS 空闲超时）；流结束/异常后若音频源未
        耗尽，阻塞等下一条首帧重连，进程生命周期内可反复恢复。"""
        while True:
            first = await self._asr_queue.get()
            if first is None:
                return
            try:
                async for ev in self._asr.stream(
                    self._asr_frames(first),
                    sample_rate=self._asr_sample_rate,
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
                self.bus.publish(
                    EventType.PIPELINE_ERROR,
                    {"stage": "asr", "error": str(e)},
                )
            if self._audio_done.is_set():
                return

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

    def _is_self_echo(self, text: str) -> bool:
        """ASR 文本是否即派蒙自己的扬声器输出被收了回来。

        无硬件 AEC 的外放场景兜底；比对"已交付 TTS"的文本
        （spoken_text——只有发出去合成过的才可能被听见）。
        戴耳机/有 AEC 时永不触发。
        """
        norm = _norm_speech(text)
        if len(norm) < _ECHO_MIN_CHARS:
            return False
        for u in self.core.context.utterances[-_ECHO_LOOKBACK:]:
            if u.discarded or u.audio_s <= 0:
                continue  # 未出声/被丢弃的内容不可能被麦克风听见
            cand = _norm_speech(u.spoken_text)
            if len(cand) < _ECHO_MIN_CHARS:
                continue
            if norm in cand or cand in norm:
                return True
            if SequenceMatcher(None, norm, cand).ratio() >= _ECHO_SIMILARITY:
                return True
        return False

    def _on_turn_complete(self, event: Event) -> None:
        text = event.payload.get("text", "")
        if (
            event.payload.get("source") != TEXT_TURN_SOURCE
            and self._is_self_echo(text)
        ):
            return  # 自听回声：不进 heard history，避免派蒙学自己说话
        self.core.context.record_user_turn(
            text,
            turn_id=event.payload.get("turn_id"),
        )

    # ---- 响应任务：LLM → chunker → TTS → 播放 ----

    def _on_can_respond(self, event: Event) -> None:
        assert self._loop is not None
        if (
            event.payload.get("source") != TEXT_TURN_SOURCE
            and self._is_self_echo(event.payload.get("text", ""))
        ):
            # 回声轮次不响应。若 USER_TURN_COMPLETE 已把状态机推进
            # THINKING，发 PLAYBACK_STOPPED 收回 IDLE（SPEAKING 中该
            # 事件本被状态机忽略，且 utterance 未封账，无需回收）。
            if self.core.state is ConversationState.THINKING:
                self.bus.publish(
                    EventType.PLAYBACK_STOPPED,
                    {"reason": "self_echo", "played_s": 0.0},
                )
            return
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

    def _on_initiative(self, event: Event) -> None:
        """INITIATIVE_TRIGGERED → 主动开口响应（"允许问一次"，LLM 可 NOOP）。

        触发到执行间有竞态：只在 IDLE 且无在途轮次/响应时真开口——其余
        情况静默丢弃这次触发（政策层已扣 cooldown，不会连环重试）。
        """
        assert self._loop is not None
        if (
            self.core.state is not ConversationState.IDLE
            or self.core.turn_manager.turn_open
            or (
                self._respond_task is not None
                and not self._respond_task.done()
            )
        ):
            return
        self._respond_task = self._loop.create_task(
            self._respond_initiative(event.payload)
        )

    async def _respond(self, turn_id: int, text: str) -> None:
        agent_input, messages, spec = self._resolve_prompt(turn_id, text)
        # 记忆 provider（可选）：检索结果覆盖进 agent_input.memory，
        # 与静态 fixture 摘要共用 system prompt 记忆槽位。
        provider = self.core.memory_provider
        if provider is not None:
            try:
                recalled = await provider.recall(text)
            except Exception:
                recalled = None
            if recalled != agent_input.get("memory"):
                agent_input["memory"] = recalled
                messages = self._agent.build_messages(agent_input)
        utterance = await self._speak(
            turn_id,
            messages,
            llm_payload={"turn_id": turn_id, "speculative": spec},
            user_text=text,
        )
        # 轮末写回：只记"实际交付"的语音文本（heard 面）
        if provider is not None and utterance is not None and text.strip():
            provider.record(text, utterance.spoken_text)

    async def _respond_initiative(self, payload: dict[str, Any]) -> None:
        """主动开口：initiative_reason 进 agent input（BehaviorPolicy 据此
        允许 NOOP）；utterance 照常开账——出声前被用户开口掐掉走六步打断。"""
        agent_input = self.core.context.build_agent_input(
            state=ConversationState.IDLE,
            last_user_text="",
            initiative_reason=payload.get("reason"),
            silence_duration_ms=payload.get("silence_ms") or 0,
        )
        messages = self._agent.build_messages(agent_input)
        await self._speak(
            None,
            messages,
            llm_payload={"initiative": True, "speculative": "none"},
        )

    async def _speak(
        self,
        turn_id: int | None,
        messages: list,
        *,
        llm_payload: dict[str, Any],
        user_text: str = "",
    ):
        """共用响应体：LLM 流 → speech 抽取 → chunker → TTS → 播放 → 封账。

        返回封账后的 utterance（供记忆写回取实际交付文本）。"""
        ctx = self.core.context
        utterance = ctx.begin_utterance()
        # 复读硬闸：归一化后与上一条 assistant 回复逐字重合的分句不送 TTS——
        # 模型在碎片输入下收敛回同一句话是实测失败模式（"鸡鸣寺"×3）。
        _prev_text = next(
            (
                str(e.get("text", ""))
                for e in reversed(ctx.logical_history)
                if e.get("role") == "assistant"
            ),
            "",
        )
        prev_norm = _norm_speech(_prev_text)

        def _is_repeat(clause: str) -> bool:
            norm = _norm_speech(clause)
            return (
                bool(prev_norm)
                and len(norm) >= _ECHO_MIN_CHARS
                and norm in prev_norm
            )
        self.bus.publish(EventType.LLM_STARTED, llm_payload)
        task = asyncio.current_task()
        if task is not None:
            self.core.interruption.track_llm(task.cancel)
        chunk_q: asyncio.Queue[str | None] = asyncio.Queue()
        audio_task: asyncio.Task | None = None
        got_token = False
        llm_attempts = 0
        try:
            chunker = ClauseChunker()
            raw_parts: list[str] = []  # 全部尝试的原始输出（诊断留痕）
            last_parts: list[str] = []  # 最后一次尝试（最终 reply 解析源）

            async def produce() -> None:
                nonlocal got_token, llm_attempts
                nonlocal last_parts
                user_norm = _norm_speech(user_text) if user_text else ""
                # 空/畸形/复读输出重试一次：实测 qwen-flash 偶发吐 [1]、[ ]
                # 这类垃圾，打断链路上还会把 user: 字段值逐字抄进 speech
                # ——普通轮这些是失败不是选择，重试一次再认命；
                # 主动开口轮（turn_id=None）沉默合法，不重试。
                # 只在"一个字都没抽出来"时重试——已有 speech 上队列说明
                # 真在说话，重试会复读。
                attempts = 1 if turn_id is None else 2
                for attempt in range(attempts):
                    llm_attempts += 1
                    extractor = SpeechFieldExtractor()
                    # 每轮新 chunker：中止的尝试不留残渣污染重试
                    chunker = ClauseChunker()
                    attempt_parts: list[str] = []
                    last_parts = attempt_parts
                    extracted = False
                    echo_aborted = False
                    parse_error: Exception | None = None
                    gen_mark = len(utterance.generated)
                    piece_buf = ""
                    # 重试带纠错提示：模型在乱序/打断上下文里系统性退化
                    # 时（实测连吐 [1]/复读 user），同样输入再发一次只会
                    # 拿同样的垃圾——显式重申契约把它拽回来。
                    stream_messages = (
                        messages
                        if attempt == 0
                        else [
                            *messages,
                            {
                                "role": "user",
                                "content": "上一条输出不合格：格式不对或在"
                                "复读对方原话。重说——用自己的话，"
                                '只输出 JSON 对象 {"speech","emotion",'
                                '"energy","should_continue"}。',
                            },
                        ]
                    )
                    async for token in self._agent.stream_reply(
                        stream_messages,
                        response_format=_JSON_OBJECT_FORMAT,
                        **self._llm_params,
                    ):
                        if not got_token:
                            got_token = True
                            self.bus.publish(
                                EventType.LLM_TOKEN,
                                {"turn_id": turn_id, "first": True},
                            )
                        attempt_parts.append(token)
                        raw_parts.append(token)
                        piece = extractor.feed(token)
                        if not piece:
                            continue
                        piece_buf += piece
                        bn = _norm_speech(piece_buf)
                        # 逐字复读闸：speech 与用户原文全等时立即中止——
                        # 必须在进 chunker 前拦，否则回声先被合成出去。
                        # 只判全等不判前缀：开头几个字撞车不等于复读
                        # （"我们明天去玄武湖" vs "我们明天去哪儿"）。
                        if user_norm and len(bn) >= 3 and bn == user_norm:
                            echo_aborted = True
                            break
                        extracted = True
                        utterance.add_generated(piece)
                        for clause in chunker.feed(piece):
                            chunk_q.put_nowait(clause)
                    if echo_aborted:
                        # 回滚已记账碎片，本轮 utterance 保持干净
                        utterance.generated = utterance.generated[:gen_mark]
                    elif extracted:
                        for clause in chunker.flush():
                            utterance.add_generated(clause)
                            chunk_q.put_nowait(clause)
                        # 流结束时仍是用户原文的长前缀 → 半截复读
                        bn = _norm_speech(piece_buf)
                        if (
                            user_norm
                            and len(bn) >= 3
                            and user_norm.startswith(bn)
                        ):
                            echo_aborted = True
                    fallback = None
                    if not extracted and not echo_aborted:
                        try:
                            fallback = parse_agent_reply(
                                "".join(attempt_parts)
                            )
                        except StructuredOutputError as e:
                            parse_error = e
                        if fallback is not None and fallback.speech:
                            fb_norm = _norm_speech(fallback.speech)
                            if (
                                user_norm
                                and len(fb_norm) >= 3
                                and user_norm.startswith(fb_norm)
                            ):
                                echo_aborted = True
                            else:
                                utterance.add_generated(fallback.speech)
                                for clause in chunker.feed(fallback.speech):
                                    chunk_q.put_nowait(clause)
                                for clause in chunker.flush():
                                    utterance.add_generated(clause)
                                    chunk_q.put_nowait(clause)
                                break
                    if extracted and not echo_aborted:
                        break
                    if attempt + 1 < attempts:
                        continue
                    if parse_error is not None:
                        raise parse_error
                    if echo_aborted:
                        raise StructuredOutputError(
                            "reply echoes user input: "
                            f"{''.join(attempt_parts)[:120]}"
                        )
                    # 最后一次尝试仍是垃圾（非契约形的非空输出）：
                    # 显式报错让前端看到，不再伪装成"她选择沉默"。
                    attempt_raw = "".join(attempt_parts).strip()
                    if attempt_raw and not _looks_like_contract(attempt_raw):
                        raise StructuredOutputError(
                            f"garbage reply after retry: {attempt_raw[:200]}"
                        )

            async def audio_out() -> None:
                first = await chunk_q.get()
                if first is None:
                    return  # NOOP：整轮没有可合成的文本
                self.bus.publish(
                    EventType.TTS_STARTED, {"turn_id": turn_id}
                )

                async def chunks() -> AsyncIterator[str]:
                    # 文本发出去的时刻即"已交付 TTS"——同步记 segment；
                    # 逐字复读上一句的分句跳过（不交付、不记 segment）。
                    if not _is_repeat(first):
                        utterance.add_segment(first, 0.0)
                        yield first
                    while True:
                        item = await chunk_q.get()
                        if item is None:
                            return
                        if _is_repeat(item):
                            continue
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

            reply = parse_agent_reply("".join(last_parts))
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
                    "llm_attempts": llm_attempts,
                    # 空回复/重试留痕：模型真选了 NOOP 还是输出了我们没接住的
                    # 格式，看 raw 一眼就能分清（latency log extra.raw）。
                    **(
                        {"raw": "".join(raw_parts)[:400]}
                        if (noop or llm_attempts > 1) and any(raw_parts)
                        else {}
                    ),
                },
            )
            await self._finish_playback(
                utterance, reason="noop" if noop else "completed"
            )
        except asyncio.CancelledError:
            # barge-in：InterruptionManager 已发 AGENT_INTERRUPTED +
            # PLAYBACK_STOPPED 并封账 heard history，这里只清理。
            # 兜底：取消路径若漏封 utterance（如未走打断的 supersede），
            # 静默丢弃——陈旧 open utterance 会把下一次开口误判成打断。
            if utterance.open and ctx.current_utterance is utterance:
                ctx.discard_current()
            raise
        except Exception as e:
            self.errors.append(f"respond: {e}")
            self.bus.publish(
                EventType.AGENT_REPLY,
                {
                    "turn_id": turn_id,
                    "speech": "",
                    "error": str(e),
                    "raw": "".join(raw_parts)[:400],
                },
            )
            await self._finish_playback(utterance, reason="error")
        finally:
            if task is not None:
                self.core.interruption.untrack_llm(task.cancel)
            if audio_task is not None and not audio_task.done():
                audio_task.cancel()
                await asyncio.gather(audio_task, return_exceptions=True)
        return utterance

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
