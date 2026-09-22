"""Smart Turn adapter：LocalSmartTurnAnalyzerV3 + TurnAnalyzerUserTurnStopStrategy。

turn-taking C1：轮次结束权在模型，不裸用 VAD 静音超时；
turn-taking C3：模型判 incomplete 后由 SmartTurnParams.stop_secs=1.2 静音
兜底 complete，且每次 complete 要可区分来源（model vs silence_fallback），
供后续 metrics 观察中文模型 fallback 率（已知中文 FNR≈9.26%）。

本 adapter 脱离完整 pipecat pipeline 独立可跑：strategy 需要的
FrameProcessorSetup 用 SystemClock + 独立 TaskManager 组装，pipeline_worker
只为 PipelineWorker 作用域状态存在，strategy 不读它，传 None。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.vad_analyzer import VAD_STOP_SECS
from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    MetricsFrame,
    StartFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import TurnMetricsData
from pipecat.processors.frame_processor import FrameDirection, FrameProcessorSetup
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.utils.asyncio.task_manager import TaskManager

# 显式配置（turn-taking C2/C3 验收点）。
SMART_TURN_PARAMS = SmartTurnParams(
    stop_secs=1.2,  # 中文误判时优先实时响应；真实聊天继续观察抢话率
    pre_speech_ms=500,
    max_duration_secs=8,
)

TurnSource = Literal["model", "silence_fallback"]


@dataclass
class TurnVerdict:
    """一次轮次判定的结果。"""

    state: EndOfTurnState
    probability: float | None
    inference_ms: float | None
    source: TurnSource | None = None

    @property
    def complete(self) -> bool:
        return self.state == EndOfTurnState.COMPLETE


class SmartTurnAdapter:
    """Smart Turn 判定封装：喂 PCM + VAD 迁移，产出 complete/incomplete。

    - `stop_strategy` 是显式接线的 TurnAnalyzerUserTurnStopStrategy，后续
      pipeline 组装（TASK-005+）直接取用。
    - `wait_for_transcript=False`：本层无 ASR，轮次结束由模型直接驱动；
      接入带转写的 pipeline 时应置 True，让 final transcript 参与门控。
    - `on_turn_complete` 回调在 strategy 真正触发轮次结束时调用，携带
      TurnVerdict（含 model/silence_fallback 来源）。
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        params: SmartTurnParams | None = None,
        cpu_count: int = 1,
        wait_for_transcript: bool = False,
    ):
        self._sample_rate = sample_rate
        self.params = params or SMART_TURN_PARAMS
        self.analyzer = LocalSmartTurnAnalyzerV3(
            params=self.params, cpu_count=cpu_count
        )
        self.stop_strategy = TurnAnalyzerUserTurnStopStrategy(
            turn_analyzer=self.analyzer,
            wait_for_transcript=wait_for_transcript,
        )
        self.on_turn_complete: Callable[[TurnVerdict], None] | None = None
        self._turn_open = False
        self._pending_source: TurnSource | None = None
        self._last_metrics: TurnMetricsData | None = None
        self._completed_verdict: TurnVerdict | None = None
        self._task_manager: TaskManager | None = None

    async def setup(self) -> None:
        """在事件循环内初始化 strategy（必须在 async 上下文调用一次）。"""
        self._task_manager = TaskManager()
        await self.stop_strategy.setup(
            FrameProcessorSetup(
                clock=SystemClock(),
                task_manager=self._task_manager,
                pipeline_worker=None,  # type: ignore[arg-type]
                audio_in_sample_rate=self._sample_rate,
            )
        )
        self.stop_strategy.add_event_handler("on_push_frame", self._on_push_frame)
        self.stop_strategy.add_event_handler(
            "on_user_turn_stopped", self._on_turn_stopped
        )
        await self.stop_strategy.process_frame(
            StartFrame(audio_in_sample_rate=self._sample_rate)
        )

    async def append_audio(self, pcm: bytes) -> None:
        """喂一帧 int16 PCM。所有音频都要喂——pre-speech ring buffer 靠它。"""
        self._pending_source = "silence_fallback"
        await self.stop_strategy.process_frame(
            InputAudioRawFrame(
                audio=pcm, sample_rate=self._sample_rate, num_channels=1
            )
        )

    async def user_speech_started(self, start_secs: float = 0.0) -> None:
        """VAD 判定用户开始说话。"""
        if not self._turn_open:
            self._turn_open = True
            await self.stop_strategy.handle_user_turn_started()
        await self.stop_strategy.process_frame(
            VADUserStartedSpeakingFrame(start_secs=start_secs)
        )

    async def user_speech_stopped(self, stop_secs: float = VAD_STOP_SECS) -> TurnVerdict:
        """VAD 判定用户停顿；strategy 在此跑模型分析，返回本次判定。"""
        self._pending_source = "model"
        self._completed_verdict = None
        await self.stop_strategy.process_frame(
            VADUserStoppedSpeakingFrame(stop_secs=stop_secs)
        )
        # 轮次在 process_frame 内触发时 _on_turn_stopped 会清掉 strategy 的
        # _turn_complete，不能直接回读；以 handler 捕获的 verdict 为准。
        if self._completed_verdict is not None:
            return self._completed_verdict
        return self._verdict(EndOfTurnState.INCOMPLETE)

    async def feed_transcript(
        self, text: str, *, finalized: bool, timestamp: str = ""
    ) -> None:
        """把 ASR 转写喂进 strategy 的转写闸门（wait_for_transcript=True 时必需）。

        模型已判 COMPLETE 时，finalized 转写到达即触发轮次结束（strategy
        `_handle_transcription`）。来源归属不动 `_pending_source`：它在
        判定路径（user_speech_stopped→model / append_audio 静音兜底→
        silence_fallback）时已盖章，转写只是放行不是判定。
        """
        frame: Frame
        if finalized:
            frame = TranscriptionFrame(
                text=text, user_id="user", timestamp=timestamp, finalized=True
            )
        else:
            frame = InterimTranscriptionFrame(
                text=text, user_id="user", timestamp=timestamp
            )
        await self.stop_strategy.process_frame(frame)

    async def clear(self) -> None:
        """丢弃当前轮次状态（外部强制结束时调用）。"""
        self._turn_open = False
        await self.stop_strategy.handle_user_turn_stopped()

    async def cleanup(self) -> None:
        await self.stop_strategy.cleanup()
        if self._task_manager:
            for task in list(self._task_manager.current_tasks()):
                await self._task_manager.cancel_task(task)

    def _verdict(self, state: EndOfTurnState) -> TurnVerdict:
        m = self._last_metrics
        return TurnVerdict(
            state=state,
            probability=m.probability if m else None,
            inference_ms=m.e2e_processing_time_ms if m else None,
            source="model" if state == EndOfTurnState.COMPLETE else None,
        )

    async def _on_push_frame(
        self, _strategy, frame: Frame, _direction: FrameDirection
    ) -> None:
        # strategy 把每次模型推理结果包成 MetricsFrame 推出来；拦下来记 verdict。
        if isinstance(frame, MetricsFrame):
            for data in frame.data:
                if isinstance(data, TurnMetricsData):
                    self._last_metrics = data

    async def _on_turn_stopped(self, _strategy, _params) -> None:
        verdict = self._verdict(EndOfTurnState.COMPLETE)
        verdict.source = self._pending_source
        self._turn_open = False
        self._completed_verdict = verdict
        await self.stop_strategy.handle_user_turn_stopped()
        if self.on_turn_complete:
            self.on_turn_complete(verdict)
