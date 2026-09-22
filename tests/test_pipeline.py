"""TASK-010 验收：脚本化 provider 驱动 VoicePipeline 端到端。

无网络/无音频设备：ScriptedVAD/Turn/ASR/LLM + ToneTTS + WavSinkPlayer
驱动真实 VoicePipeline + ConversationCore，断言：

- doc 06 §3 九时间戳逐轮齐 + SEFA；
- ASR partial → prompt 预构造 → turn_complete 命中（speculative=hit）；
- miss/none 分支（_resolve_prompt 单元断言 + e2e 日志字段）；
- barge-in：SPEAKING 中用户开口 → 打断封账 + barge_in_stop_ms；
- NOOP：speech 为空 → 不合成不播放，状态机经 PLAYBACK_STOPPED 回 IDLE。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from character.agent import CharacterAgent
from conversation.core import ConversationCore
from conversation.events import EventType
from conversation.state_machine import ConversationState
from metrics.latency import NINE_TIMESTAMPS, LatencyLog
from providers.llm.base import AgentReply
from runtime.pipeline import (
    ClauseChunker,
    SpeechFieldExtractor,
    VoicePipeline,
)
from runtime.simulated import (
    ScriptedASR,
    ScriptedLLM,
    ScriptedTurn,
    ScriptedVAD,
    ToneTTS,
    WavSinkPlayer,
    list_frames,
    silence_frame,
    tone_frame,
)

REPLY = {
    "speech": "哈？你现在才发现？派蒙早知道了！",
    "emotion": "smug",
    "energy": 0.7,
    "should_continue": False,
}


def make_pipeline(
    *,
    frames,
    vad_segments,
    transcripts,
    reply=REPLY,
    turn=None,
    llm=None,
    tts=None,
    player=None,
    tmp_path=None,
    settle_s=0.15,
):
    """全脚本化 pipeline 组装；返回 (pipeline, metrics, player, tts, llm)。"""
    vad = ScriptedVAD(vad_segments)
    turn = turn or ScriptedTurn()
    asr = ScriptedASR(transcripts)
    llm = llm or ScriptedLLM(reply)
    tts = tts or ToneTTS(sample_rate=24000, secs_per_chunk=0.05)
    player = player or WavSinkPlayer(sample_rate=24000, realtime=False)
    core = ConversationCore(playback=player, tts=tts)
    metrics = LatencyLog(
        core.bus, outdir=tmp_path if tmp_path is not None else None
    )
    pipeline = VoicePipeline(
        audio=list_frames(frames),
        vad=vad,
        turn=turn,
        asr=asr,
        agent=CharacterAgent(llm),
        tts=tts,
        player=player,
        core=core,
        metrics=metrics,
        auto_stop=True,
        auto_stop_settle_s=settle_s,
    )
    return pipeline, metrics, player, tts, llm


async def run_pipeline(pipeline, timeout=10.0):
    await asyncio.wait_for(pipeline.run(), timeout=timeout)


# ---------------------------------------------------------------- happy path


async def test_end_to_end_turn_records_all_timestamps(tmp_path):
    """一句话 → 可听回复链路：九时间戳齐、SEFA 正、speculative=hit。"""
    frames = (
        [silence_frame() for _ in range(2)]
        + [tone_frame() for _ in range(26)]
        + [silence_frame() for _ in range(20)]
    )
    pipeline, metrics, player, tts, _ = make_pipeline(
        frames=frames,
        vad_segments=[(2, 28)],
        transcripts="我觉得这个项目吧",
        # release_on_final：与真实 wait_for_transcript 同语义——
        # 判定 complete 后等 ASR final 放行，t_asr_final 先于 confirmed。
        turn=ScriptedTurn(release_on_final=True),
        tmp_path=tmp_path,
    )
    await run_pipeline(pipeline)

    assert not pipeline.errors
    closed = [r for r in metrics.records if r.closed]
    assert len(closed) == 1
    rec = closed[0]
    for key in (
        "t_user_speech_end",
        "t_turn_confirmed",
        "t_asr_final",
        "t_llm_first_token",
        "t_tts_request",
        "t_first_audio",
        "t_playback_start",
        "t_playback_stopped",
    ):
        assert getattr(rec, key) is not None, f"{key} missing"
    # 无打断：interrupt_detected 为空
    assert rec.t_interrupt_detected is None
    assert rec.sefa_ms is not None and rec.sefa_ms > 0
    assert rec.speculative == "hit"
    assert rec.prompt_prebuilt_at is not None
    assert rec.stop_reason == "completed"
    assert rec.reply_speech == REPLY["speech"]
    assert player.written_seconds > 0
    assert tts.synthesized  # 语义块进入了 TTS

    # 落盘 JSONL：九字段键全部存在（None 也显式落盘）
    assert metrics.path is not None and metrics.path.exists()
    rows = [
        json.loads(l)
        for l in metrics.path.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    assert len(rows) == 1
    for key in NINE_TIMESTAMPS:
        assert key in rows[0]
    assert rows[0]["sefa_ms"] is not None
    assert rows[0]["speculative"] == "hit"

    # heard history：完整播出 → heard == generated
    heard = pipeline.core.context.heard_history
    assert any(
        e["role"] == "assistant" and e["text"] == REPLY["speech"]
        for e in heard
    )
    assert pipeline.core.state == ConversationState.IDLE


async def test_json_string_reply_falls_back_to_speech_and_tts(tmp_path):
    speech = "嘿，还在发呆吗？"
    frames = [tone_frame() for _ in range(26)] + [silence_frame() for _ in range(20)]
    pipeline, metrics, player, tts, _ = make_pipeline(
        frames=frames,
        vad_segments=[(0, 26)],
        transcripts="派蒙你好",
        reply=json.dumps(speech, ensure_ascii=False),
        turn=ScriptedTurn(release_on_final=True),
        tmp_path=tmp_path,
    )

    await run_pipeline(pipeline)

    assert not pipeline.errors
    assert metrics.records[-1].reply_speech == speech
    assert tts.synthesized
    assert player.written_seconds > 0


# ---------------------------------------------------------------- 自听回声


def _seed_assistant_speech(pipeline, text: str, audio_s: float = 3.0) -> None:
    """预置一条"派蒙刚说过"的封账 utterance（模拟扬声器播过的内容）。"""
    ctx = pipeline.core.context
    u = ctx.begin_utterance()
    u.add_generated(text)
    u.add_segment(text, audio_s)
    ctx.seal_current()


async def test_self_echo_turn_is_dropped(tmp_path):
    """ASR 收回派蒙自己的声音 → 整轮丢弃：不进历史、不发 LLM、收回 IDLE。"""
    frames = (
        [silence_frame() for _ in range(2)]
        + [tone_frame() for _ in range(26)]
        + [silence_frame() for _ in range(20)]
    )
    pipeline, metrics, player, tts, llm = make_pipeline(
        frames=frames,
        vad_segments=[(2, 28)],
        transcripts="你看那里有一只穿着皮衣的奶龙",
        turn=ScriptedTurn(release_on_final=True),
        tmp_path=tmp_path,
    )
    ctx = pipeline.core.context
    _seed_assistant_speech(pipeline, "你看那里有一只穿着皮衣的奶龙。")

    await run_pipeline(pipeline)

    assert not pipeline.errors
    assert not llm.requests  # 回声轮次不发给 LLM
    assert not any(e["role"] == "user" for e in ctx.heard_history)
    closed = [r for r in metrics.records if r.closed]
    assert closed and closed[-1].stop_reason == "self_echo"
    assert pipeline.core.state == ConversationState.IDLE


async def test_non_echo_turn_after_speech_still_responds(tmp_path):
    """真用户发言与派蒙刚说的话不相似 → 回声闸门不误伤。"""
    frames = (
        [silence_frame() for _ in range(2)]
        + [tone_frame() for _ in range(26)]
        + [silence_frame() for _ in range(20)]
    )
    pipeline, metrics, player, tts, llm = make_pipeline(
        frames=frames,
        vad_segments=[(2, 28)],
        transcripts="今天天气怎么样",
        turn=ScriptedTurn(release_on_final=True),
        tmp_path=tmp_path,
    )
    ctx = pipeline.core.context
    _seed_assistant_speech(pipeline, "你看那里有一只穿着皮衣的奶龙。")

    await run_pipeline(pipeline)

    assert not pipeline.errors
    assert llm.requests  # 正常发给 LLM
    assert any(e["role"] == "user" for e in ctx.heard_history)
    assert metrics.records[-1].reply_speech == REPLY["speech"]


# ---------------------------------------------------------------- 投机分支


async def test_resolve_prompt_hit_miss_none():
    """投机 hit/miss/none 三分支（_resolve_prompt 单元断言）。

    e2e 中 partial 预构造在 final 刷新后仍命中即 hit；miss 是"预构造存在
    但文本被后续事件改写"（如完成时刻文本≠预构造文本）——单测直接驱动。
    """
    pipeline, *_ = make_pipeline(
        frames=[], vad_segments=[], transcripts="x", tmp_path=None
    )
    ctx = pipeline.core.context
    agent_input = ctx.build_agent_input(
        state=ConversationState.THINKING, last_user_text="你好"
    )
    pipeline._prebuilt = {
        "turn_id": 1,
        "text": "你好",
        "agent_input": agent_input,
        "messages": [{"role": "user", "content": "你好"}],
    }
    _, msgs, spec = pipeline._resolve_prompt(1, "你好")
    assert spec == "hit"
    assert msgs == [{"role": "user", "content": "你好"}]

    pipeline._prebuilt = {
        "turn_id": 1,
        "text": "你好",
        "agent_input": agent_input,
        "messages": [],
    }
    _, msgs, spec = pipeline._resolve_prompt(1, "你好呀")
    assert spec == "miss"
    assert msgs and msgs[-1]["role"] == "user"

    spec_none = pipeline._resolve_prompt(2, "另一轮")
    assert spec_none[2] == "none"


async def test_speculative_hit_e2e_flag():
    """e2e：partial 驱动的预构造被 turn_complete 复用（LLM 只见一份请求）。"""
    frames = (
        [silence_frame() for _ in range(2)]
        + [tone_frame() for _ in range(26)]
        + [silence_frame() for _ in range(20)]
    )
    pipeline, metrics, *_ = make_pipeline(
        frames=frames,
        vad_segments=[(2, 28)],
        transcripts="我觉得这个项目吧",
        turn=ScriptedTurn(release_on_final=True),
    )
    await run_pipeline(pipeline)
    rec = metrics.records[-1]
    assert rec.speculative == "hit"
    # PROMPT_PREBUILT 事件发生在 USER_TURN_COMPLETE 之前（预构造先行）
    types = [e.type for e in pipeline.bus.history]
    assert types.index(EventType.PROMPT_PREBUILT) < types.index(
        EventType.USER_TURN_COMPLETE
    )


# ---------------------------------------------------------------- barge-in


async def test_barge_in_interrupts_and_records(tmp_path):
    """SPEAKING 中用户重新开口：六步打断、heard 截断、barge_in_stop_ms。"""
    # 布局：turn1 speech 帧 2..15；sleep 后 turn2 speech 帧 ~30..40
    turn1_frames = [silence_frame()] * 2 + [tone_frame()] * 13
    turn2_frames = [tone_frame()] * 11 + [silence_frame()] * 15

    async def audio():
        for f in turn1_frames + [silence_frame()] * 6:
            yield f
        await asyncio.sleep(0.4)  # 让 turn1 响应进入 SPEAKING/播放中
        for f in turn2_frames:
            yield f

    vad = ScriptedVAD([(2, 15), (21, 32)])
    turn = ScriptedTurn()
    # 首个 speech 帧即吐全句 partial：打断场景里泵与 ASR 消费天然赛跑，
    # 用 partial_every=1 保证轮次完成前 _last_text 已定。
    asr = ScriptedASR(
        # 第二轮文本避开静默指令词（"你闭嘴"会触发 TASK-011 SILENCED 语义）
        ["我觉得这个项目吧", "换一个话题"],
        partial_every=1,
        chars_per_partial=99,
    )
    llm = ScriptedLLM(REPLY, token_size=4, token_delay_s=0.01)
    # 长合成 + 实时落点：barge-in 时仍有 pending audio
    tts = ToneTTS(sample_rate=24000, secs_per_chunk=1.2)
    player = WavSinkPlayer(sample_rate=24000, realtime=True)
    core = ConversationCore(playback=player, tts=tts)
    metrics = LatencyLog(core.bus, outdir=tmp_path)
    pipeline = VoicePipeline(
        audio=audio(),
        vad=vad,
        turn=turn,
        asr=asr,
        agent=CharacterAgent(llm),
        tts=tts,
        player=player,
        core=core,
        metrics=metrics,
        auto_stop=True,
        auto_stop_settle_s=0.2,
    )
    await asyncio.wait_for(pipeline.run(), timeout=15)

    closed = [r for r in metrics.records if r.closed]
    assert len(closed) >= 2
    first, second = closed[0], closed[1]
    # 被打断轮：interrupt + stopped 齐、barge_in_stop_ms 给出
    assert first.stop_reason == "interrupted"
    assert first.t_interrupt_detected is not None
    assert first.t_playback_stopped is not None
    assert first.barge_in_stop_ms is not None and first.barge_in_stop_ms >= 0
    # 打断轮次的 heard history 截断标记
    interrupted_entries = [
        e
        for e in pipeline.core.context.heard_history
        if e.get("role") == "assistant" and e.get("interrupted")
    ]
    assert interrupted_entries, "heard history missing interrupted entry"
    assert tts.cancelled_count >= 1  # TTS 被取消
    # 第二轮正常完成
    assert second.stop_reason == "completed"
    assert second.user_text == "换一个话题"


# ---------------------------------------------------------------- NOOP


async def test_noop_reply_skips_tts_and_returns_idle():
    """speech 为空 → 不走 TTS/播放；THINKING 经 PLAYBACK_STOPPED 回 IDLE。"""
    frames = (
        [silence_frame() for _ in range(2)]
        + [tone_frame() for _ in range(10)]
        + [silence_frame() for _ in range(15)]
    )
    pipeline, metrics, player, tts, _llm = make_pipeline(
        frames=frames,
        vad_segments=[(2, 12)],
        transcripts="嗯",
        reply={"speech": "", "emotion": "neutral", "energy": 0.0},
    )
    await run_pipeline(pipeline)

    assert player.written_seconds == 0
    assert tts.synthesized == []
    rec = metrics.records[-1]
    assert rec.closed and rec.stop_reason == "noop"
    assert rec.t_tts_request is None and rec.t_first_audio is None
    assert pipeline.core.state == ConversationState.IDLE


# ---------------------------------------------------------------- ASR 懒连接/重连


class _FlakyASR:
    """前 fail_times 次连接即抛的假 ASR；之后消费完帧流吐一条 final。"""

    def __init__(self, fail_times: int = 0) -> None:
        self.calls = 0
        self._fail_times = fail_times

    def stream(self, frames, *, sample_rate):
        self.calls += 1
        call = self.calls

        async def gen():
            if call <= self._fail_times:
                raise RuntimeError(f"asr boom #{call}")
            async for _ in frames:
                pass
            yield SimpleNamespace(kind="final", text=f"第{call}连", raw=None)

        return gen()


async def test_asr_loop_lazy_connects_on_first_pcm():
    """空闲期不开 ASR socket：首帧 PCM 进队前 provider.stream 不得被调用。"""
    pipeline, *_ = make_pipeline(
        frames=[], vad_segments=[], transcripts="x"
    )
    fake = _FlakyASR()
    pipeline._asr = fake

    task = asyncio.create_task(pipeline._asr_loop())
    try:
        await asyncio.sleep(0.05)
        assert fake.calls == 0, "stream opened before first PCM frame"

        pipeline._asr_queue.put_nowait(b"\x00" * 4)
        for _ in range(200):
            if fake.calls:
                break
            await asyncio.sleep(0.01)
        assert fake.calls == 1
    finally:
        pipeline._audio_done.set()
        pipeline._asr_queue.put_nowait(None)
        await asyncio.wait_for(task, timeout=5)


async def test_asr_loop_reconnects_after_provider_error():
    """provider 异常 → 记 error + PIPELINE_ERROR；下一条 PCM 触发重连，
    新流照常产出 ASR_FINAL（发布后喂转写闸门）。"""
    pipeline, *_ = make_pipeline(
        frames=[], vad_segments=[], transcripts="x"
    )
    fake = _FlakyASR(fail_times=1)
    pipeline._asr = fake

    task = asyncio.create_task(pipeline._asr_loop())
    try:
        pipeline._asr_queue.put_nowait(b"\x00" * 4)
        for _ in range(200):
            if pipeline.errors:
                break
            await asyncio.sleep(0.01)
        assert fake.calls == 1
        assert pipeline.errors and "asr" in pipeline.errors[0]
        assert any(
            e.type == EventType.PIPELINE_ERROR
            and e.payload.get("stage") == "asr"
            for e in pipeline.bus.history
        )

        pipeline._asr_queue.put_nowait(b"\x00" * 4)
        pipeline._audio_done.set()
        pipeline._asr_queue.put_nowait(None)
        await asyncio.wait_for(task, timeout=5)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert fake.calls == 2
    finals = [
        e for e in pipeline.bus.history if e.type == EventType.ASR_FINAL
    ]
    assert finals and finals[-1].payload["text"] == "第2连"


# ---------------------------------------------------------------- 组件单测


def test_speech_field_extractor():
    ex = SpeechFieldExtractor()
    text = '{"speech": "哈？你现在", "emotion": "smug", "energy": 0.7}'
    pieces = [ex.feed(text[i : i + 5]) for i in range(0, len(text), 5)]
    assert "".join(pieces) == "哈？你现在"


def test_speech_field_extractor_escapes_and_split_key():
    ex = SpeechFieldExtractor()
    chunks = ['{"spe', 'ech": "笑', '话\\n第', '二句\\u3002', '"}']
    out = "".join(ex.feed(c) for c in chunks)
    assert out == "笑话\n第二句。"


def test_clause_chunker_boundaries():
    ch = ClauseChunker()
    assert ch.feed("哈？你现在才发现") == ["哈？"]
    assert ch.feed("？派蒙早知道了！") == ["你现在才发现？", "派蒙早知道了！"]
    assert ch.flush() == []
    ch2 = ClauseChunker()
    assert ch2.feed("没有标点的一整句") == []
    assert ch2.flush() == ["没有标点的一整句"]


async def test_verbatim_repeat_of_last_reply_is_muted(tmp_path):
    """复读硬闸：新回复与上一条 assistant 逐字重合的分句不送 TTS——
    模型在碎片输入下复读是实测失败模式，宁可沉默也不原样再念一遍。"""
    frames = (
        [silence_frame() for _ in range(2)]
        + [tone_frame() for _ in range(26)]
        + [silence_frame() for _ in range(20)]
    )
    pipeline, metrics, player, tts, _ = make_pipeline(
        frames=frames,
        vad_segments=[(2, 28)],
        transcripts="嗯。",
        reply=REPLY,  # 回复与历史里的上一条完全相同
        tmp_path=tmp_path,
    )
    # 种一条内容相同的上轮回复进历史
    pipeline.core.context.logical_history.append(
        {
            "role": "assistant",
            "text": REPLY["speech"],
            "heard_text": REPLY["speech"],
            "interrupted": False,
            "utterance_id": 0,
        }
    )
    await run_pipeline(pipeline)

    assert not pipeline.errors
    # 逐字复读的长分句被闸掉：交付 TTS 的 spoken_text 不含复述的句子；
    # 短语气词（"哈？"<4 字）放行是刻意的——真人也会先"哈？"一下
    utterance = pipeline.core.context.utterances[-1]
    assert "你现在才发现" not in utterance.spoken_text
    assert "派蒙早知道了" not in utterance.spoken_text
    heard = [
        e
        for e in pipeline.core.context.heard_history
        if e.get("role") == "assistant"
    ]
    assert not heard or "你现在才发现" not in heard[-1].get("text", "")


async def test_fresh_reply_not_muted_by_history(tmp_path):
    """对照组：不复读的正常回复照常出声。"""
    frames = (
        [silence_frame() for _ in range(2)]
        + [tone_frame() for _ in range(26)]
        + [silence_frame() for _ in range(20)]
    )
    pipeline, _, player, _, _ = make_pipeline(
        frames=frames,
        vad_segments=[(2, 28)],
        transcripts="你说什么",
        reply=REPLY,
        tmp_path=tmp_path,
    )
    pipeline.core.context.logical_history.append(
        {
            "role": "assistant",
            "text": "完全不同的上一句",
            "heard_text": "完全不同的上一句",
            "interrupted": False,
            "utterance_id": 0,
        }
    )
    await run_pipeline(pipeline)

    assert player.written_seconds > 0


async def test_garbage_llm_output_retries_once(tmp_path):
    """实测缺陷：qwen-flash 偶发吐 [1]/[ ] 这类垃圾——普通轮静默 NOOP
    是失败不是选择，自动重试一次，第二次的真回复要出声。"""
    calls = []

    def reply_fn(_messages):
        calls.append(1)
        return "[1]" if len(calls) == 1 else REPLY

    frames = (
        [silence_frame() for _ in range(2)]
        + [tone_frame() for _ in range(26)]
        + [silence_frame() for _ in range(20)]
    )
    pipeline, metrics, player, _, llm = make_pipeline(
        frames=frames,
        vad_segments=[(2, 28)],
        transcripts="还记得什么",
        llm=ScriptedLLM(reply_fn),
        tmp_path=tmp_path,
    )
    await run_pipeline(pipeline)

    assert len(llm.requests) == 2  # 重试了一次
    assert player.written_seconds > 0  # 重试的回复出声了
    closed = [r for r in metrics.records if r.closed]
    assert closed[-1].reply_speech == REPLY["speech"]
    assert closed[-1].extra.get("llm_attempts") == 2
    assert "[1]" in closed[-1].extra.get("raw", "")  # 首轮垃圾留痕


async def test_garbage_twice_surfaces_error(tmp_path):
    """两次都吐垃圾 → 不伪装沉默：显式 error + raw 留痕可查。"""
    frames = (
        [silence_frame() for _ in range(2)]
        + [tone_frame() for _ in range(26)]
        + [silence_frame() for _ in range(20)]
    )
    pipeline, metrics, player, _, llm = make_pipeline(
        frames=frames,
        vad_segments=[(2, 28)],
        transcripts="还记得什么",
        llm=ScriptedLLM("[1]"),
        tmp_path=tmp_path,
    )
    await run_pipeline(pipeline)

    assert len(llm.requests) == 2
    assert player.written_seconds == 0
    closed = [r for r in metrics.records if r.closed]
    assert closed[-1].reply_speech == ""
    assert closed[-1].stop_reason == "error"  # 垃圾输出不再装成沉默
    assert "[1]" in closed[-1].extra.get("raw", "")
    assert any("garbage reply" in e for e in pipeline.errors)
