"""TASK-010 验收：TerminalUI 输出对 doc 06 §2 形态的断言。

用真实 ConversationCore + LatencyLog + TerminalUI(StringIO)，bus 事件按
pipeline 真实时序驱动两轮：一轮正常一问一答（含投机 hit），第二轮进入
SPEAKING 后 barge-in，验证 `[INTERRUPTED +Nms]` 与 `[LISTENING]` 恢复行。
"""

from __future__ import annotations

import io
import re

from conversation.core import ConversationCore
from conversation.events import EventType as ET
from metrics.latency import LatencyLog
from runtime.terminal import TerminalUI


class FakePlayer:
    """InterruptionManager 的 PlaybackLike：stop() 返回已播秒数。"""

    def __init__(self) -> None:
        self.stopped = 0

    def stop(self) -> float:
        self.stopped += 1
        return 0.6


class FakeTTS:
    def __init__(self) -> None:
        self.cancelled = 0

    def cancel(self) -> None:
        self.cancelled += 1


def make_ui():
    player, tts = FakePlayer(), FakeTTS()
    core = ConversationCore(playback=player, tts=tts)
    metrics = LatencyLog(core.bus)
    buf = io.StringIO()
    TerminalUI(core.bus, metrics, out=buf)
    return core, metrics, buf, player, tts


def drive_turn(core, turn_id: int, text: str, speech: str) -> None:
    """按 pipeline 真实时序发一轮完整对话事件（SPEAKING 后停在该态）。"""
    bus = core.bus
    bus.publish(ET.USER_SPEECH_STARTED)
    bus.publish(ET.ASR_PARTIAL, {"text": text[:4]})
    bus.publish(ET.ASR_PARTIAL, {"text": text})
    bus.publish(ET.PROMPT_PREBUILT, {"turn_id": turn_id, "text": text})
    bus.publish(ET.USER_SPEECH_STOPPED)
    bus.publish(ET.ASR_FINAL, {"text": text})
    bus.publish(ET.TURN_COMPLETE, {"source": "model", "probability": 0.9})
    bus.publish(ET.LLM_STARTED, {"turn_id": turn_id, "speculative": "hit"})
    bus.publish(ET.LLM_TOKEN, {"turn_id": turn_id, "first": True})
    bus.publish(ET.TTS_STARTED, {"turn_id": turn_id})
    bus.publish(ET.FIRST_AUDIO, {"turn_id": turn_id})
    bus.publish(ET.AGENT_SPEAKING, {"turn_id": turn_id})
    bus.publish(
        ET.AGENT_REPLY,
        {
            "turn_id": turn_id,
            "speech": speech,
            "emotion": "smug",
            "energy": 0.7,
            "should_continue": False,
            "noop": False,
        },
    )


def test_terminal_doc06_section2_format():
    """正常轮：状态行 + YOU/Turn/延迟三行/PAIMON 全部按 doc §2 出现。"""
    core, metrics, buf, player, _tts = make_ui()
    drive_turn(core, 1, "我觉得这个项目吧", "哈？你现在才发现？")
    core.bus.publish(
        ET.PLAYBACK_STOPPED, {"reason": "completed", "played_s": 1.1}
    )

    out = buf.getvalue()
    assert "[LISTENING]" in out
    assert "YOU: 我觉得这个项目吧" in out
    assert "Turn: COMPLETE" in out
    assert re.search(r"ASR final: \+\d+ms", out)
    assert re.search(r"LLM first token: \+\d+ms", out)
    assert re.search(r"TTS first audio: \+\d+ms", out)
    assert "PAIMON (smug):" in out
    assert "哈？你现在才发现？" in out
    assert "[SPEAKING]" in out
    assert "[IDLE]" in out
    # latency 三行先于 PAIMON 文本（doc §2 顺序）
    assert out.index("TTS first audio:") < out.index("PAIMON")
    # 投机缘测行：partial 预构造 + speculation=hit 都要可见
    assert "(prompt prebuilt" in out
    assert "(llm request, speculation=hit)" in out

    rec = metrics.records[-1]
    assert rec.closed and rec.stop_reason == "completed"
    assert rec.sefa_ms is not None and rec.speculative == "hit"


def test_terminal_barge_in_interrupted_line():
    """SPEAKING 中用户开口：`[INTERRUPTED +Nms]` 带数字一行 + 回到 LISTENING。"""
    core, metrics, buf, player, tts = make_ui()
    drive_turn(core, 1, "你觉得今天吃什么", "派蒙觉得——")

    # 播放中：补一个在途 utterance（pipeline 里由 _respond 建），然后打断
    utt = core.context.begin_utterance()
    utt.add_generated("派蒙觉得——")
    utt.add_segment("派蒙觉得——", 2.0)
    buf.truncate(0)
    buf.seek(0)
    core.bus.publish(ET.USER_SPEECH_STARTED)  # barge-in

    out = buf.getvalue()
    assert re.search(r"\[INTERRUPTED \+\d+ms\]", out), out
    # doc §2：打断行后回到 LISTENING；且没有裸 [INTERRUPTED] 残留
    assert "[LISTENING]" in out
    assert "[INTERRUPTED]\n" not in out
    assert out.index("[INTERRUPTED") < out.index("[LISTENING]")

    assert player.stopped == 1 and tts.cancelled == 1
    rec = metrics.records[0]
    assert rec.stop_reason == "interrupted"
    assert rec.barge_in_stop_ms is not None and rec.barge_in_stop_ms >= 0
