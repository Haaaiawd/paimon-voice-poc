"""TASK-004：VAD / Smart Turn adapter 的显式配置与离线判定链路。

不依赖麦克风：直接喂 PCM 走 strategy → analyzer → ONNX 的完整路径，
覆盖 C1（TurnAnalyzerUserTurnStopStrategy + LocalSmartTurnAnalyzerV3 接线）
与 C3（模型 incomplete 后 stop_secs=3.0 静音兜底 complete，且来源可区分）。
"""

from __future__ import annotations

import pytest
from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.vad_analyzer import VADState
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)

from turn.smart_turn_adapter import SMART_TURN_PARAMS, SmartTurnAdapter
from turn.vad_adapter import VAD_PARAMS, SileroVADAdapter

SAMPLE_RATE = 16000
FRAME = 512 * 2  # 32ms int16 mono bytes


def test_vad_explicit_stop_secs():
    """C2：VAD stop_secs=0.2 显式配置，交给模型判定轮次。"""
    assert VAD_PARAMS.stop_secs == 0.2
    vad = SileroVADAdapter()
    assert vad.params.stop_secs == 0.2


async def test_vad_silence_is_quiet():
    vad = SileroVADAdapter()
    res = await vad.analyze(b"\x00" * FRAME)
    assert res.state == VADState.QUIET
    assert not res.user_speaking and not res.started and not res.stopped
    await vad.cleanup()


def test_smart_turn_wiring_and_params():
    """C1/C2：strategy + analyzer 接线与显式参数。"""
    adapter = SmartTurnAdapter()
    assert isinstance(adapter.analyzer, LocalSmartTurnAnalyzerV3)
    assert isinstance(adapter.stop_strategy, TurnAnalyzerUserTurnStopStrategy)
    assert adapter.stop_strategy._turn_analyzer is adapter.analyzer
    assert SMART_TURN_PARAMS.stop_secs == 3.0
    assert SMART_TURN_PARAMS.pre_speech_ms == 500
    assert SMART_TURN_PARAMS.max_duration_secs == 8
    assert adapter.params is SMART_TURN_PARAMS


async def test_model_verdict_drives_turn_end():
    """真实 ONNX 推理路径：VAD stop 触发 analyze，complete 时轮次结束。

    输入固定 → ONNX 结果确定；本输入下模型判 complete（实测 p>0.5）。
    """
    adapter = SmartTurnAdapter()
    await adapter.setup()
    verdicts = []
    adapter.on_turn_complete = verdicts.append

    await adapter.user_speech_started()
    for _ in range(10):  # ~0.3s "speech"（is_speech 由 VAD 帧驱动）
        await adapter.append_audio(b"\x11\x22" * 512)

    verdict = await adapter.user_speech_stopped(0.2)
    assert verdict.state == EndOfTurnState.COMPLETE
    assert verdict.probability is not None
    assert verdict.source == "model"
    assert verdicts and verdicts[0].complete
    await adapter.cleanup()


async def test_silence_fallback_completes_turn():
    """C3：模型判 incomplete 后，3.0s 静音兜底判 complete，来源可区分。"""
    adapter = SmartTurnAdapter()
    # 钉住模型输出，只测 strategy/analyzer 的兜底接线，不测模型本身。
    adapter.analyzer._predict_endpoint = lambda audio: {
        "prediction": 0,
        "probability": 0.1,
    }
    await adapter.setup()
    verdicts = []
    adapter.on_turn_complete = verdicts.append

    await adapter.user_speech_started()
    for _ in range(10):
        await adapter.append_audio(b"\x11\x22" * 512)

    verdict = await adapter.user_speech_stopped(0.2)
    assert verdict.state == EndOfTurnState.INCOMPLETE
    assert not verdicts

    # 模型判 incomplete → 继续喂静音帧，累计超过 stop_secs=3.0s 兜底
    for _ in range(int(3.2 / 0.032)):
        await adapter.append_audio(b"\x00" * FRAME)
        if verdicts:
            break

    assert verdicts, "turn never completed"
    assert verdicts[0].complete
    assert verdicts[0].source == "silence_fallback"
    await adapter.cleanup()
