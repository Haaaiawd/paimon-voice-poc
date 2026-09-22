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
