"""TASK-004 acceptance 2：播放器流式 write() + stop() 立即清 buffer + 位置回报。

verify_by: pytest tests/test_playback.py 验证 stop 延迟与播放位置回报。

用 FakeStream 替代真实音频设备（本机无声卡；Windows 目标机走
sd.RawOutputStream），回放位置与 stop 语义走同一条代码路径。
"""

from __future__ import annotations

import time

import pytest
import sounddevice as sd

from runtime.playback import StreamingPlayer

SAMPLE_RATE = 24000
BYTES_PER_SEC = SAMPLE_RATE * 2  # mono int16


class FakeStream:
    """模拟 sd.RawOutputStream：工厂即自身，测试手动 pump 消费音频。"""

    frame_bytes = 2

    def __init__(self):
        self.callback = None
        self.started = False
        self.aborted = False
        self.closed = False

    def __call__(self, callback):
        self.callback = callback
        return self

    def start(self):
        self.started = True

    def abort(self):
        self.aborted = True

    def close(self):
        self.closed = True

    def pump(self, frames: int) -> bytes:
        """模拟 PortAudio 回调消费 frames 帧，返回实际输出内容。"""
        out = bytearray(frames * self.frame_bytes)
        assert self.callback is not None
        self.callback(memoryview(out), frames, None, None)
        return bytes(out)


def _pcm(seconds: float, byte: int = 0x11) -> bytes:
    return bytes([byte]) * int(seconds * BYTES_PER_SEC)


def test_position_reporting():
    fake = FakeStream()
    player = StreamingPlayer(sample_rate=SAMPLE_RATE, stream_factory=fake)

    player.write(_pcm(1.0))
    assert fake.started  # 首次 write 惰性开流
    assert player.pending_seconds == pytest.approx(1.0)

    fake.pump(int(SAMPLE_RATE * 0.2))  # 消费 0.2s
    assert player.position_seconds == pytest.approx(0.2)
    assert player.pending_seconds == pytest.approx(0.8)

    played = player.stop()
    assert played == pytest.approx(0.2)
    player.close()


def test_stop_clears_buffer_and_aborts_immediately():
    fake = FakeStream()
    player = StreamingPlayer(sample_rate=SAMPLE_RATE, stream_factory=fake)
    player.write(_pcm(2.0))
    fake.pump(int(SAMPLE_RATE * 0.1))

    t0 = time.monotonic()
    played = player.stop()
    latency = time.monotonic() - t0

    assert fake.aborted and fake.closed
    assert latency < 0.05  # stop 只做清 buffer + abort，不排空
    assert played == pytest.approx(0.1)
    assert player.pending_seconds == 0.0
    assert player.position_seconds == 0.0  # 计数归零，返回值承载结果

    # buffer 已清：回调再被调用只能输出静音，无残音
    assert set(fake.pump(1024)) == {0}
    player.close()


def test_stop_is_idempotent_and_safe_when_idle():
    player = StreamingPlayer(sample_rate=SAMPLE_RATE, stream_factory=FakeStream())
    assert player.stop() == 0.0  # 未播过：no-op
    player.write(_pcm(0.5))
    player.stop()
    assert player.stop() == 0.0  # 重复 stop 无副作用
    player.close()


def test_write_after_stop_starts_fresh_session():
    streams: list[FakeStream] = []

    def factory(cb):
        s = FakeStream()
        s(cb)
        streams.append(s)
        return s

    player = StreamingPlayer(sample_rate=SAMPLE_RATE, stream_factory=factory)
    player.write(_pcm(1.0))
    streams[0].pump(int(SAMPLE_RATE * 0.3))
    assert player.stop() == pytest.approx(0.3)

    player.write(_pcm(1.0))  # 新一轮：新流 + 位置归零
    assert len(streams) == 2 and streams[1].started
    assert player.position_seconds == 0.0
    streams[1].pump(int(SAMPLE_RATE * 0.5))
    assert player.position_seconds == pytest.approx(0.5)
    player.close()


def _has_output_device() -> bool:
    try:
        return sd.default.device[1] is not None and sd.default.device[1] >= 0
    except Exception:
        return False


@pytest.mark.skipif(not _has_output_device(), reason="no output device on this host")
def test_real_device_play_and_stop():
    player = StreamingPlayer(sample_rate=SAMPLE_RATE)
    player.write(_pcm(0.5))
    time.sleep(0.1)
    played = player.stop()
    assert 0 < played < 0.5
    player.close()
