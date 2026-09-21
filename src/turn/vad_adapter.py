"""Silero VAD adapter：PCM -> speech/non-speech 判定与 started/stopped 迁移事件。

turn-taking C2：VAD 只回答"现在有没有人在说话"，stop_secs 取官方推荐的
短值 0.2s，让停顿事件尽早产生，把"说完了吗"交给 Smart Turn 模型；不要
把 stop_secs 拉长当轮次超时用（无模型时才需要长 stop_secs）。
"""

from __future__ import annotations

from dataclasses import dataclass

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams, VADState

# 显式配置（turn-taking C2 验收点）：句中停顿/拖长尾音靠 Smart Turn 判定，
# VAD 只负责尽快报出停顿。
VAD_PARAMS = VADParams(stop_secs=0.2)


@dataclass
class VADResult:
    """单次 analyze() 的结果。

    state: 当前 VAD 状态（QUIET/STARTING/SPEAKING/STOPPING）。
    user_speaking: 本轮 VAD 帧处理后的"用户说话中"窗口标记——
        SPEAKING/STOPPING 期间为 True，等价于 pipecat 管线里
        VADUserStartedSpeakingFrame 与 VADUserStoppedSpeakingFrame 之间。
    started/stopped: 本帧是否触发了 started/stopped 迁移。
    """

    state: VADState
    user_speaking: bool
    started: bool = False
    stopped: bool = False


class SileroVADAdapter:
    """Silero VAD 的薄封装：吃 int16 PCM bytes，产出 VADResult。"""

    def __init__(self, *, sample_rate: int = 16000, params: VADParams | None = None):
        self._analyzer = SileroVADAnalyzer(
            sample_rate=sample_rate, params=params or VAD_PARAMS
        )
        # 构造函数只锁定采样率；set_sample_rate 才真正初始化帧长/状态机。
        self._analyzer.set_sample_rate(sample_rate)
        self._user_speaking = False

    @property
    def analyzer(self) -> SileroVADAnalyzer:
        return self._analyzer

    @property
    def params(self) -> VADParams:
        return self._analyzer.params

    @property
    def user_speaking(self) -> bool:
        return self._user_speaking

    async def analyze(self, pcm: bytes) -> VADResult:
        state = await self._analyzer.analyze_audio(pcm)
        started = state == VADState.SPEAKING and not self._user_speaking
        stopped = state == VADState.QUIET and self._user_speaking
        self._user_speaking = state in (VADState.SPEAKING, VADState.STOPPING)
        return VADResult(
            state=state,
            user_speaking=self._user_speaking,
            started=started,
            stopped=stopped,
        )

    async def cleanup(self) -> None:
        await self._analyzer.cleanup()
