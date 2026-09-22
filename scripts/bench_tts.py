#!/usr/bin/env python3
"""TASK-008：TTS 赛马 benchmark —— Fish Audio(s2.1-pro-free) vs 百炼 cosyvoice-v3-flash。

对每个已配置 key 的 provider 各跑 --runs 轮（默认 10）：
- 热 TTFA 探针：WS 常驻连接（Fish 靠会话间预热、百炼靠连接复用），
  从 stream_audio(chunks) 调用到首字节音频的端到端时间（mean/p50/p95）；
- 取消探针：首音后立即 cancel()，断言无残音产出并立刻重开成功，
  计取消成功率与 cancel 耗时。

测句集为派蒙域中文（短句吐槽/语气词/反问句占大头，chinese-tts-eval C4），
文本按标点切成语义块送入——与真实管线 Text Chunker 同形。

key 未配置的 provider 标注 MISSING_KEY 并跳过；零 key 时整体 blocked
（退出码 2），仍把结果落盘到 data/tts_benchmark/。

用法：.venv/bin/python scripts/bench_tts.py [--runs 10] [--cancels 5]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import dotenv_values  # noqa: E402

from providers.tts import (  # noqa: E402
    BailianCosyVoiceTTS,
    FishAudioTTS,
    TTSProvider,
)

MISSING_KEY = "MISSING_KEY"
STATUS_OK = "ok"
STATUS_BLOCKED = "blocked"
STATUS_ERROR = "ERROR"

DEFAULT_RUNS = 10
DEFAULT_CANCELS = 5
DEFAULT_OUTDIR = ROOT / "data" / "tts_benchmark"
DEFAULT_ENV_FILE = ROOT / ".env"

# 赛马名单（D-014）：Fish s2.1-pro-free 免费层 vs 百炼 cosyvoice-v3-flash。
PROVIDER_SPECS: tuple[dict[str, str], ...] = (
    {
        "name": "fish",
        "model": "s2.1-pro-free",
        "key_env": "FISH_API_KEY",
        "model_env": "FISH_TTS_MODEL",
    },
    {
        "name": "bailian",
        "model": "cosyvoice-v3-flash",
        "key_env": "DASHSCOPE_API_KEY",
        "model_env": "BAILIAN_TTS_MODEL",
    },
)

# 派蒙域测句集（C4）：短句吐槽/语气词/反问句为主，含长句与标点变化。
SENTENCES: tuple[str, ...] = (
    "诶——你怎么现在才来？我等得花儿都谢了！",
    "哼，这点小问题，交给派蒙就好啦。",
    "旅行者，你说那个宝箱会不会有陷阱呀？",
    "哇！是甜甜花酿鸡！快给我留一口，就一口！",
    "唔……让我想想，这个地方我们好像来过的样子？",
    "喂！不要突然不说话嘛，怪吓人的。",
    "今天的冒险也拜托你啦，派蒙会帮你加油的！",
    "你、你说什么？派蒙才不是应急食品呢！",
)


def split_chunks(sentence: str) -> list[str]:
    """模拟 Text Chunker：按中文标点切语义块（标点随前块）。"""
    parts = re.split(r"(?<=[，。！？；、：…—])", sentence)
    return [p for p in parts if p]


async def _chunks_of(sentence: str) -> AsyncIterator[str]:
    """真实管线同形：语义块即产即送（不等整段）。"""
    for c in split_chunks(sentence):
        yield c
        await asyncio.sleep(0)  # 让出调度，模拟异步到达


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    model: str
    key_env: str
    api_key: str | None

    @property
    def status(self) -> str:
        return STATUS_OK if self.api_key else MISSING_KEY


def load_env(env_file: Path) -> dict[str, str]:
    merged = {
        k: v
        for k, v in (dotenv_values(env_file) if env_file.exists() else {}).items()
        if v is not None
    }
    for spec in PROVIDER_SPECS:
        for var in (spec["key_env"], spec["model_env"]):
            if var in os.environ:
                merged[var] = os.environ[var]
    return merged


def resolve_providers(env: Mapping[str, str]) -> list[ProviderSpec]:
    providers = []
    for spec in PROVIDER_SPECS:
        key = (env.get(spec["key_env"]) or "").strip() or None
        model = (env.get(spec["model_env"]) or "").strip() or spec["model"]
        providers.append(
            ProviderSpec(
                name=spec["name"],
                model=model,
                key_env=spec["key_env"],
                api_key=key,
            )
        )
    return providers


def make_tts(spec: ProviderSpec) -> TTSProvider:
    if spec.name == "fish":
        return FishAudioTTS(api_key=spec.api_key or "", model=spec.model)
    if spec.name == "bailian":
        return BailianCosyVoiceTTS(api_key=spec.api_key or "", model=spec.model)
    raise ValueError(f"unknown provider {spec.name}")


def percentile(values: list[float], q: float) -> float:
    """线性插值分位数（numpy 默认口径）。values 无需有序。"""
    if not values:
        raise ValueError("percentile of empty sample")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def summarize(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": round(sum(values) / len(values), 1),
        "p50": round(percentile(values, 0.50), 1),
        "p95": round(percentile(values, 0.95), 1),
    }


async def _synth_once(
    tts: TTSProvider, sentence: str
) -> tuple[float, float, int, int]:
    """一轮合成，返回 (ttfa_ms, total_ms, audio_bytes, audio_chunks)。"""
    t0 = time.perf_counter()
    ttfa: float | None = None
    nbytes = 0
    nchunks = 0
    async for audio in tts.stream_audio(_chunks_of(sentence)):
        if ttfa is None:
            ttfa = (time.perf_counter() - t0) * 1000
        nbytes += len(audio)
        nchunks += 1
    total = (time.perf_counter() - t0) * 1000
    if ttfa is None:
        raise RuntimeError("stream completed with zero audio")
    return ttfa, total, nbytes, nchunks


async def _cancel_once(
    tts: TTSProvider, sentence: str
) -> tuple[bool, float, int, str | None]:
    """一次取消探针：首音后 cancel，返回 (成功?, cancel_ms, 残音帧数, 备注)。

    成功 = cancel 返回后迭代不再产出音频 且 立即重开的新合成能出音频。
    """
    got = 0
    residual = 0
    try:
        async for audio in tts.stream_audio(_chunks_of(sentence)):
            got += 1
            if got == 1:
                t0 = time.perf_counter()
                await tts.cancel()
                cancel_ms = (time.perf_counter() - t0) * 1000
            elif got > 1:
                residual += 1  # cancel 之后还在出帧 = 残音
    except Exception as e:
        return False, -1.0, residual, f"stream/cancel raised {type(e).__name__}: {e}"[:200]
    if got == 0:
        return False, -1.0, residual, "no audio before cancel"
    # 立刻重开：必须是干净的新合成
    try:
        ttfa, _, nbytes, _ = await _synth_once(tts, "好的，重来。")
        if nbytes == 0:
            return False, cancel_ms, residual, "reopen produced zero audio"
    except Exception as e:
        return False, cancel_ms, residual, f"reopen failed {type(e).__name__}: {e}"[:200]
    note = None
    if residual:
        note = f"residual chunks after cancel: {residual}"
    return True, cancel_ms, residual, note


async def bench_provider(
    spec: ProviderSpec,
    tts: TTSProvider,
    *,
    runs: int,
    cancels: int,
    interval: float = 0.0,
) -> dict[str, Any]:
    """对单个 provider 跑 warmup + runs×TTFA + cancels×取消探针。"""
    result: dict[str, Any] = {
        "name": spec.name,
        "model": spec.model,
        "key_env": spec.key_env,
        "status": STATUS_OK,
    }
    notes: list[str] = []

    try:
        warm_ttfa, _, _, _ = await _synth_once(tts, SENTENCES[0])
        result["warmup_ttfa_ms"] = round(warm_ttfa, 1)  # 冷启动（含建连）
    except Exception as e:  # 预热失败（鉴权/网络）→ 重试一次再判死刑
        await asyncio.sleep(1.0)
        try:
            await _synth_once(tts, SENTENCES[0])
        except Exception as e2:
            result["status"] = STATUS_ERROR
            result["error"] = f"{type(e2).__name__}: {e2}"[:300]
            return result

    ttfa_samples: list[float] = []
    total_samples: list[float] = []
    warm_acquires = 0
    ttfa_errors: list[str] = []

    for i in range(runs):
        sentence = SENTENCES[i % len(SENTENCES)]
        try:
            ttfa, total, nbytes, nchunks = await _synth_once(tts, sentence)
            ttfa_samples.append(ttfa)
            total_samples.append(total)
            warm = getattr(tts, "last_acquire_warm", None)
            warm_acquires += bool(warm)
            result.setdefault("samples", []).append(
                {
                    "i": i,
                    "sentence": sentence,
                    "ttfa_ms": round(ttfa, 1),
                    "total_ms": round(total, 1),
                    "audio_bytes": nbytes,
                    "audio_chunks": nchunks,
                    "ws_warm": warm,
                }
            )
        except Exception as e:
            ttfa_errors.append(f"{type(e).__name__}: {e}"[:200])
        if interval:
            await asyncio.sleep(interval)

    cancel_stat = {"attempts": 0, "success": 0, "residual_total": 0}
    cancel_ms_samples: list[float] = []
    for i in range(cancels):
        ok, cms, residual, note = await _cancel_once(
            tts, SENTENCES[(i + 3) % len(SENTENCES)]
        )
        cancel_stat["attempts"] += 1
        cancel_stat["success"] += int(ok)
        cancel_stat["residual_total"] += residual
        if cms >= 0:
            cancel_ms_samples.append(cms)
        if note:
            notes.append(f"cancel#{i}: {note}")
        if interval:
            await asyncio.sleep(interval)

    result["ttfa_ms"] = summarize(ttfa_samples)
    result["total_ms"] = summarize(total_samples)
    result["warm_acquires"] = warm_acquires
    cancel_stat["rate"] = (
        round(cancel_stat["success"] / cancel_stat["attempts"], 3)
        if cancel_stat["attempts"]
        else None
    )
    cancel_stat["cancel_ms"] = summarize(cancel_ms_samples)
    result["cancel"] = cancel_stat
    if ttfa_errors:
        result["ttfa_errors"] = ttfa_errors
        if not ttfa_samples:
            result["status"] = STATUS_ERROR
    if warm_acquires < len(ttfa_samples):
        notes.append(
            f"{len(ttfa_samples) - warm_acquires}/{len(ttfa_samples)} 轮走了冷连接"
        )
    if notes:
        result["notes"] = notes
    return result


def render_table(results: list[dict[str, Any]]) -> str:
    header = (
        "| provider | model               | runs | ttfa_mean | ttfa_p50 | ttfa_p95 "
        "| total_p50 | warm | cancel_ok | status     |"
    )
    sep = "|" + "|".join("-" * w for w in (10, 21, 6, 11, 10, 10, 11, 6, 11, 12)) + "|"
    lines = [header, sep]
    for r in results:
        t, tot, c = (
            r.get("ttfa_ms", {}),
            r.get("total_ms", {}),
            r.get("cancel", {}),
        )

        def fmt(d: dict, k: str) -> str:
            v = d.get(k)
            return f"{v:.0f}" if isinstance(v, (int, float)) else "-"

        rate = c.get("rate")
        lines.append(
            "| {name:<8} | {model:<19} | {runs:>4} | {tm:>9} | {tp:>8} | {t95:>8} "
            "| {fp:>9} | {wm:>4} | {sr:>9} | {status:<10} |".format(
                name=r["name"],
                model=str(r.get("model", "-"))[:19],
                runs=t.get("n", 0),
                tm=fmt(t, "mean"),
                tp=fmt(t, "p50"),
                t95=fmt(t, "p95"),
                fp=fmt(tot, "p50"),
                wm=r.get("warm_acquires", "-"),
                sr=(f"{rate:.0%}" if isinstance(rate, (int, float)) else "-"),
                status=r["status"],
            )
        )
    return "\n".join(lines)


def _display_path(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def pick_winner(results: list[dict[str, Any]]) -> str | None:
    """热 TTFA p50 更低者胜出；两家都 ok 才裁决，否则 None。"""
    ok = [
        r
        for r in results
        if r["status"] == STATUS_OK and r.get("ttfa_ms", {}).get("p50") is not None
    ]
    if len(ok) < 2:
        return ok[0]["name"] if len(ok) == 1 else None
    return min(ok, key=lambda r: r["ttfa_ms"]["p50"])["name"]


def write_artifacts(report: dict[str, Any], outdir: Path) -> list[str]:
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    paths = []
    json_path = outdir / f"bench_{stamp}.json"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    paths.append(_display_path(json_path))
    latest = outdir / "latest.json"
    latest.write_text(json_path.read_text(encoding="utf-8"), encoding="utf-8")
    paths.append(_display_path(latest))
    md_lines = [
        f"# TASK-008 TTS benchmark — {report['generated_at']}",
        "",
        f"status: **{report['status']}**",
        "",
        render_table(report["providers"]),
        "",
    ]
    if report.get("winner"):
        md_lines.append(f"winner（热 TTFA p50）: **{report['winner']}**")
        md_lines.append("")
    for r in report["providers"]:
        for note in r.get("notes", []):
            md_lines.append(f"- {r['name']}: {note}")
        for err in r.get("ttfa_errors", [])[:5]:
            md_lines.append(f"- {r['name']} error: {err}")
    (outdir / "latest.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    paths.append(_display_path(outdir / "latest.md"))
    return paths


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    env = load_env(Path(args.env_file))
    providers = resolve_providers(env)
    ready = [p for p in providers if p.api_key]

    results: list[dict[str, Any]] = []
    for spec in providers:
        if not spec.api_key:
            results.append(
                {
                    "name": spec.name,
                    "model": spec.model,
                    "key_env": spec.key_env,
                    "status": MISSING_KEY,
                }
            )
            continue
        tts = make_tts(spec)
        try:
            results.append(
                await bench_provider(
                    spec,
                    tts,
                    runs=args.runs,
                    cancels=args.cancels,
                    interval=args.interval,
                )
            )
        finally:
            close = getattr(tts, "close", None)
            if close is not None:
                await close()

    status = STATUS_BLOCKED if not ready else STATUS_OK
    if ready and all(
        r["status"] == STATUS_ERROR for r in results if r["status"] != MISSING_KEY
    ):
        status = STATUS_ERROR
    return {
        "task": "TASK-008",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runs_per_provider": args.runs,
        "cancel_probes": args.cancels,
        "status": status,
        "winner": pick_winner(results),
        "providers": results,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    p.add_argument("--cancels", type=int, default=DEFAULT_CANCELS)
    p.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    p.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    p.add_argument("--interval", type=float, default=0.3)
    args = p.parse_args(argv)

    report = asyncio.run(_run(args))
    artifacts = write_artifacts(report, args.outdir)

    print(render_table(report["providers"]))
    print(f"\nstatus: {report['status']}  winner: {report.get('winner')}")
    for a in artifacts:
        print(f"wrote {a}")
    if report["status"] == STATUS_BLOCKED:
        missing = [
            p["key_env"] for p in report["providers"] if p["status"] == MISSING_KEY
        ]
        print(
            f"\nblocked: 零可用 key，缺失环境变量 {missing}；"
            "在 .env 填入任意一家后重跑。"
        )
        return 2
    return 0 if report["status"] == STATUS_OK else 1


if __name__ == "__main__":
    raise SystemExit(main())
