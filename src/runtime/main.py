"""TASK-010 端到端入口：Mic → VAD → Smart Turn → ASR → Core → LLM → TTS → 播放。

用法：

    # 真实链路（需有效 API key + 音频设备；doc 06 §2 的终端形态）
    .venv/bin/python -m runtime.main

    # 文件输入 + 真实 VAD/SmartTurn + 脚本化 ASR/LLM/TTS（无 key 演示）
    .venv/bin/python -m runtime.main --mock --real-turn --file speech.pcm

    # 全脚本化确定性回放（CI/无音频设备环境）
    .venv/bin/python -m runtime.main --mock

key 从 .env 读（dotenv_values），缺 key 时明示并退出码 2（与 bench 脚本
同口径）；无输出设备自动降级 WavSinkPlayer 并把"播出"PCM 落盘 wav。
延迟账本落盘 data/latency_log/session_*.jsonl + summary_*.json。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import dotenv_values

from character.agent import CharacterAgent
from conversation.core import ConversationCore
from memory.loader import load_memory
from metrics.latency import LatencyLog
from runtime.pipeline import VoicePipeline
from runtime.simulated import (
    ScriptedASR,
    ScriptedLLM,
    ScriptedTurn,
    ScriptedVAD,
    ToneTTS,
    WavSinkPlayer,
    pcm_file_frames,
    silence_frame,
    tone_frame,
)
from runtime.terminal import TerminalUI

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"
DEFAULT_OUTDIR = ROOT / "data" / "latency_log"

#: mock 模式的默认脚本（可被 --text/--reply 覆盖）。
MOCK_TRANSCRIPT = "我觉得这个项目吧，好像越来越有意思了。"
MOCK_REPLY = {
    "speech": "哈？你现在才发现？派蒙早就觉得这项目有戏了！",
    "emotion": "smug",
    "energy": 0.7,
    "should_continue": False,
}
MOCK_TOTAL_FRAMES = 120  # ~3.8s @32ms
MOCK_SPEECH = (8, 80)  # 帧区间


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m runtime.main",
        description="Paimon Voice PoC end-to-end pipeline (TASK-010)",
    )
    p.add_argument("--file", type=Path, help="int16 16kHz mono PCM 裸流输入")
    p.add_argument(
        "--mock", action="store_true", help="ASR/LLM/TTS 用脚本化实现"
    )
    p.add_argument(
        "--real-turn",
        action="store_true",
        help="mock 模式下仍用真实 Silero VAD + Smart Turn（需真实语音文件）",
    )
    p.add_argument("--text", help="mock ASR 的转写文本")
    p.add_argument("--reply", help="mock LLM 的 speech 字段文本")
    p.add_argument("--once", action="store_true", help="音频源耗尽后自动退出")
    p.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    p.add_argument(
        "--playback-wav",
        type=Path,
        help="WavSinkPlayer 落盘路径（无声卡时默认 data/playback.wav）",
    )
    p.add_argument("--llm-model", help="覆盖 LLM model 名")
    p.add_argument(
        "--no-terminal", action="store_true", help="关闭终端 UI（留 metrics）"
    )
    p.add_argument(
        "--min-barge-in-s", type=float, default=0.0, help="最短打断时长门槛"
    )
    p.add_argument("--verbose", action="store_true", help="保留 pipecat DEBUG 日志")
    return p.parse_args()


def _load_env() -> dict[str, str]:
    import os

    env = {k: v for k, v in dotenv_values(ENV_FILE).items() if v is not None}
    for k in (
        "DASHSCOPE_API_KEY",
        "FISH_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_COMPATIBLE_URL",
        "DASHSCOPE_WS_URL",
        "QWEN_MODEL",
        "TTS_PROVIDER",
        "TTS_VOICE",
        "MEMORY_DIR",
    ):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def _mock_audio():
    async def frames():
        for i in range(MOCK_TOTAL_FRAMES):
            if MOCK_SPEECH[0] <= i < MOCK_SPEECH[1]:
                yield tone_frame()
            else:
                yield silence_frame()

    return frames()


def _build_mock(args):
    """脚本化 ASR/LLM/TTS + （可选）脚本化 VAD/Turn 与音频源。"""
    transcript = args.text or MOCK_TRANSCRIPT
    reply = dict(MOCK_REPLY)
    if args.reply:
        reply["speech"] = args.reply
    asr = ScriptedASR(transcript)
    llm = ScriptedLLM(reply)
    tts = ToneTTS(sample_rate=24000)
    if args.real_turn:
        from turn.smart_turn_adapter import SmartTurnAdapter
        from turn.vad_adapter import SileroVADAdapter

        vad = SileroVADAdapter()
        turn = SmartTurnAdapter(wait_for_transcript=True)
        if args.file is None:
            raise SystemExit("--mock --real-turn 需要 --file 提供真实语音 PCM")
    else:
        vad = ScriptedVAD([MOCK_SPEECH])
        turn = ScriptedTurn()
    if args.file is not None:
        audio = pcm_file_frames(args.file)
    else:
        audio = _mock_audio()
    return audio, vad, turn, asr, llm, tts


def _build_real(args, env):
    from providers.asr import DashScopeASR
    from providers.llm import OpenAICompatibleLLM
    from providers.tts import BailianCosyVoiceTTS, FishAudioTTS
    from turn.smart_turn_adapter import SmartTurnAdapter
    from turn.vad_adapter import SileroVADAdapter

    dash_key = env.get("DASHSCOPE_API_KEY", "")
    fish_key = env.get("FISH_API_KEY", "")
    missing = []
    if not dash_key:
        missing.append("DASHSCOPE_API_KEY（ASR + 备用 TTS + 百炼 LLM）")
    llm_base = env.get(
        "OPENAI_COMPATIBLE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    llm_model = args.llm_model or env.get("QWEN_MODEL", "qwen-flash")
    if missing:
        raise SystemExit(
            "缺少 API key：" + "；".join(missing) + "。填 .env 或用 --mock 演示。"
        )
    ws_url = env.get("DASHSCOPE_WS_URL")  # 可覆盖默认 api-ws 端点（relay/私有网关）
    vad = SileroVADAdapter()
    turn = SmartTurnAdapter(wait_for_transcript=True)
    asr = DashScopeASR(api_key=dash_key, **({"url": ws_url} if ws_url else {}))
    llm = OpenAICompatibleLLM(
        base_url=llm_base, api_key=dash_key, model=llm_model
    )
    # TTS_PROVIDER 显式选型：默认 bailian——Fish 跨境握手在本网络环境
    # 实测持续超时（D-014 赛马结果）；海外/VPS 环境可设 TTS_PROVIDER=fish。
    tts_kind = env.get("TTS_PROVIDER", "bailian").lower()
    if tts_kind == "fish":
        if not fish_key:
            raise SystemExit("TTS_PROVIDER=fish 但缺少 FISH_API_KEY。")
        tts = FishAudioTTS(api_key=fish_key)
    else:
        tts = BailianCosyVoiceTTS(
            api_key=dash_key,
            **({"url": ws_url} if ws_url else {}),
            **({"voice": env["TTS_VOICE"]} if env.get("TTS_VOICE") else {}),
        )
    if args.file is not None:
        audio = pcm_file_frames(args.file)
    else:
        from runtime.mic import MicCapture

        mic = MicCapture(sample_rate=16000, blocksize=512)
        mic_ctx = mic

        class _MicSource:
            async def __aiter__(self):
                mic_ctx.start()
                async for f in mic_ctx.frames():
                    yield f

        audio = _MicSource()
    return audio, vad, turn, asr, llm, tts


def _build_player(args, tts):
    """有声卡 → StreamingPlayer；无声卡 → WavSinkPlayer 落盘 wav。"""
    if args.mock:
        wav = args.playback_wav or ROOT / "data" / "playback_mock.wav"
        return WavSinkPlayer(
            sample_rate=tts.sample_rate, out_path=wav, realtime=True
        )
    try:
        import sounddevice as sd

        default_out = sd.default.device[1]
        if default_out is None or int(default_out) < 0:
            raise RuntimeError("no default output device")
        from runtime.playback import StreamingPlayer

        return StreamingPlayer(sample_rate=tts.sample_rate)
    except Exception as e:
        wav = args.playback_wav or ROOT / "data" / "playback.wav"
        print(
            f"[playback] no output device ({e}) → WavSinkPlayer 落盘 {wav}",
            file=sys.stderr,
        )
        return WavSinkPlayer(
            sample_rate=tts.sample_rate, out_path=wav, realtime=True
        )


async def _run(args) -> int:
    env = _load_env()
    if args.mock:
        audio, vad, turn, asr, llm, tts = _build_mock(args)
    else:
        audio, vad, turn, asr, llm, tts = _build_real(args, env)
    player = _build_player(args, tts)
    agent = CharacterAgent(llm)

    memory_dir = env.get("MEMORY_DIR") or (ROOT / "memory")
    pack = load_memory(memory_dir)
    memory_provider = None
    memory_text = pack.text
    if (env.get("MEMORY_PROVIDER") or "").lower() == "mem0":
        from memory.mem0_provider import Mem0MemoryProvider

        memory_provider = Mem0MemoryProvider(
            api_key=env["DASHSCOPE_API_KEY"],
            base_url=env.get("OPENAI_COMPATIBLE_URL")
            or "https://dashscope.aliyuncs.com/compatible-mode/v1",
            llm_model=env.get("QWEN_MODEL") or "qwen-flash",
            embed_model=env.get("MEM0_EMBED_MODEL") or "text-embedding-v3",
            persist_dir=env.get("MEM0_DIR") or (ROOT / "data" / "mem0"),
        )
        memory_provider.seed_fixture(memory_dir)
        memory_text = ""
    core = ConversationCore(
        playback=player,
        tts=tts,
        min_barge_in_s=args.min_barge_in_s,
        memory_text=memory_text,
        memory_provider=memory_provider,
    )
    metrics = LatencyLog(core.bus, outdir=args.outdir)
    pipeline = VoicePipeline(
        audio=audio,
        vad=vad,
        turn=turn,
        asr=asr,
        agent=agent,
        tts=tts,
        player=player,
        core=core,
        metrics=metrics,
        auto_stop=args.once or args.file is not None or args.mock,
    )
    if not args.no_terminal:
        TerminalUI(pipeline.bus, metrics)
    close_llm = getattr(llm, "close", None)

    print("[pipeline] started (Ctrl+C 退出)" if not args.mock else
          "[pipeline] mock run started", file=sys.stderr)
    try:
        await pipeline.run()
    except KeyboardInterrupt:
        pass
    finally:
        if close_llm is not None:
            try:
                await close_llm()
            except Exception:
                pass
    summary_path = metrics.close()
    for e in pipeline.errors:
        print(f"[pipeline error] {e}", file=sys.stderr)
    if metrics.path:
        print(f"[metrics] log → {metrics.path}", file=sys.stderr)
    if summary_path:
        print(f"[metrics] summary → {summary_path}", file=sys.stderr)
        import json as _json

        summary = _json.loads(summary_path.read_text(encoding="utf-8"))
        print(
            f"[metrics] turns={summary['turns']} "
            f"sefa={summary['sefa_ms']} "
            f"speculative={summary['speculative']}",
            file=sys.stderr,
        )
    return 0


def main() -> None:
    # Windows 控制台 GBK 中文乱码防护（F-032）
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = _parse_args()
    if not args.verbose:
        try:
            from loguru import logger

            logger.remove()
            logger.add(sys.stderr, level="WARNING")
        except Exception:
            pass
    try:
        raise SystemExit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        raise SystemExit(0)


if __name__ == "__main__":
    main()
