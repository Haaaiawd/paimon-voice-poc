#!/usr/bin/env python3
"""TASK-003：LLM 赛马 benchmark —— DeepSeek vs 通义千问（OpenAI-compatible）。

对每个已配置 key 的 endpoint 各跑 --runs 次（默认 20）：
- TTFT 探针：普通 streaming chat，计首 token 延迟（mean/p50/p95）与总时延；
- 结构化探针：response_format=json_object，按 providers.llm.parse_agent_reply
  判定结构化输出成功率——即线上真实消费口径。

key 未配置的 endpoint 标注 MISSING_KEY 并跳过；零 key 时整体 blocked（退出码 2），
仍把结果落盘到 data/llm_benchmark/。只做测量，不做选型结论。

用法：.venv/bin/python scripts/bench_llm.py [--runs 20] [--outdir data/llm_benchmark]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import dotenv_values  # noqa: E402

from providers.llm import (  # noqa: E402
    LLMProvider,
    OpenAICompatibleLLM,
    StructuredOutputError,
)

MISSING_KEY = "MISSING_KEY"
STATUS_OK = "ok"
STATUS_BLOCKED = "blocked"
STATUS_ERROR = "ERROR"

DEFAULT_RUNS = 20
DEFAULT_OUTDIR = ROOT / "data" / "llm_benchmark"
DEFAULT_ENV_FILE = ROOT / ".env"

# 赛马名单（D-004）：两家都是 OpenAI-compatible，差异只在 base_url/model/key。
# model 可被同名 *_MODEL 环境变量覆盖。
ENDPOINT_SPECS: tuple[dict[str, str], ...] = (
    {
        "name": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "key_env": "DEEPSEEK_API_KEY",
        "model_env": "DEEPSEEK_MODEL",
    },
    {
        "name": "qwen",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.5-omni-flash",
        "key_env": "DASHSCOPE_API_KEY",
        "model_env": "QWEN_MODEL",
    },
)

# 与线上负载同形：派蒙人格 system + 中文口语短句 user。
SYSTEM_PROMPT = (
    "你是派蒙，旅行者的伙伴。用简短、口语化、带点得意的中文回答，"
    "一两句话以内，不要书面腔。"
)

TTFT_PROMPTS: tuple[str, ...] = (
    "你觉得今天吃什么？",
    "你烦不烦？",
    "我觉得这个项目吧……其实还挺有意思的。",
    "你最好想清楚再说。",
    "派蒙，给我讲个冷笑话。",
)

STRUCTURED_INSTRUCTION = (
    '严格只输出一个 JSON 对象，字段：{"speech": string, '
    '"emotion": one of neutral|happy|excited|teasing|annoyed|confused|smug|soft, '
    '"energy": 0到1的小数, "should_continue": bool}。不要输出任何其他文字。'
)

MAX_TOKENS = 96


@dataclass(frozen=True)
class Endpoint:
    name: str
    base_url: str
    model: str
    key_env: str
    api_key: str | None

    @property
    def status(self) -> str:
        return STATUS_OK if self.api_key else MISSING_KEY


def load_env(env_file: Path) -> dict[str, str]:
    """.env 为基础，真实环境变量优先；两者都提供 name→value 视图。"""
    merged = {
        k: v
        for k, v in (dotenv_values(env_file) if env_file.exists() else {}).items()
        if v is not None
    }
    for spec in ENDPOINT_SPECS:
        for var in (spec["key_env"], spec["model_env"]):
            if var in os.environ:
                merged[var] = os.environ[var]
    return merged


def resolve_endpoints(env: Mapping[str, str]) -> list[Endpoint]:
    """把环境变量解析成 endpoint 列表；无 key 的保留条目并标 MISSING_KEY。"""
    endpoints = []
    for spec in ENDPOINT_SPECS:
        key = (env.get(spec["key_env"]) or "").strip() or None
        model = (env.get(spec["model_env"]) or "").strip() or spec["model"]
        endpoints.append(
            Endpoint(
                name=spec["name"],
                base_url=spec["base_url"],
                model=model,
                key_env=spec["key_env"],
                api_key=key,
            )
        )
    return endpoints


def make_llm(
    endpoint: Endpoint, *, timeout: float, trust_env: bool
) -> OpenAICompatibleLLM:
    return OpenAICompatibleLLM(
        base_url=endpoint.base_url,
        api_key=endpoint.api_key or "",
        model=endpoint.model,
        timeout=timeout,
        trust_env=trust_env,
    )


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


async def _ttft_once(
    llm: LLMProvider, user_prompt: str
) -> tuple[float, float, int]:
    """一次 streaming 调用，返回 (ttft_ms, total_ms, token_chunks)。"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    t0 = time.perf_counter()
    ttft: float | None = None
    chunks = 0
    async for token in llm.stream_reply(messages, max_tokens=MAX_TOKENS):
        if ttft is None:
            ttft = time.perf_counter() - t0
        chunks += 1
    total = time.perf_counter() - t0
    if ttft is None:
        raise StructuredOutputError("stream completed with zero tokens")
    return ttft * 1000, total * 1000, chunks


async def _structured_once(llm: LLMProvider, user_prompt: str) -> float:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + "\n" + STRUCTURED_INSTRUCTION},
        {"role": "user", "content": user_prompt},
    ]
    t0 = time.perf_counter()
    await llm.complete_structured(messages, max_tokens=MAX_TOKENS)
    return (time.perf_counter() - t0) * 1000


async def bench_endpoint(
    endpoint: Endpoint,
    llm: LLMProvider,
    *,
    runs: int,
    interval: float = 0.0,
) -> dict[str, Any]:
    """对单个 endpoint 跑 warmup + runs×(TTFT+结构化) 探针，返回结果 dict。"""
    result: dict[str, Any] = {
        "name": endpoint.name,
        "base_url": endpoint.base_url,
        "model": endpoint.model,
        "key_env": endpoint.key_env,
        "status": STATUS_OK,
    }

    try:
        warm_ttft, _, _ = await _ttft_once(llm, TTFT_PROMPTS[0])
        result["warmup_ttft_ms"] = round(warm_ttft, 1)
    except Exception as e:  # 预热失败（鉴权/网络）→ 重试一次再判死刑
        await asyncio.sleep(1.0)
        try:
            await _ttft_once(llm, TTFT_PROMPTS[0])
        except Exception as e2:
            result["status"] = STATUS_ERROR
            result["error"] = f"{type(e2).__name__}: {e2}"[:300]
            return result

    ttft_samples: list[float] = []
    total_samples: list[float] = []
    ttft_errors: list[str] = []
    structured = {"attempts": 0, "success": 0, "parse_errors": 0, "transport_errors": 0}

    for i in range(runs):
        prompt = TTFT_PROMPTS[i % len(TTFT_PROMPTS)]
        try:
            ttft, total, chunks = await _ttft_once(llm, prompt)
            ttft_samples.append(ttft)
            total_samples.append(total)
            result.setdefault("samples", []).append(
                {
                    "i": i,
                    "prompt": prompt,
                    "ttft_ms": round(ttft, 1),
                    "total_ms": round(total, 1),
                    "chunks": chunks,
                }
            )
        except Exception as e:
            ttft_errors.append(f"{type(e).__name__}: {e}"[:200])
        if interval:
            await asyncio.sleep(interval)

        structured["attempts"] += 1
        try:
            await _structured_once(llm, prompt)
            structured["success"] += 1
        except StructuredOutputError:
            structured["parse_errors"] += 1
        except Exception:
            structured["transport_errors"] += 1
        if interval:
            await asyncio.sleep(interval)

    result["ttft_ms"] = summarize(ttft_samples)
    result["total_ms"] = summarize(total_samples)
    structured["rate"] = (
        round(structured["success"] / structured["attempts"], 3)
        if structured["attempts"]
        else None
    )
    result["structured"] = structured
    if ttft_errors:
        result["ttft_errors"] = ttft_errors
        result["status"] = STATUS_ERROR if not ttft_samples else STATUS_OK
    return result


def render_table(results: list[dict[str, Any]]) -> str:
    header = (
        "| provider  | model           | runs | ttft_mean | ttft_p50 | ttft_p95 "
        "| total_p50 | total_p95 | struct_ok | status     |"
    )
    sep = "|" + "|".join("-" * w for w in (11, 17, 6, 11, 10, 10, 11, 11, 11, 12)) + "|"
    lines = [header, sep]
    for r in results:
        t, tot, st = r.get("ttft_ms", {}), r.get("total_ms", {}), r.get("structured", {})

        def fmt(d: dict, k: str) -> str:
            v = d.get(k)
            return f"{v:.0f}" if isinstance(v, (int, float)) else "-"

        rate = st.get("rate")
        lines.append(
            "| {name:<9} | {model:<15} | {runs:>4} | {tm:>9} | {tp:>8} | {t95:>8} "
            "| {fp:>9} | {f95:>9} | {sr:>9} | {status:<10} |".format(
                name=r["name"],
                model=str(r.get("model", "-"))[:15],
                runs=t.get("n", 0),
                tm=fmt(t, "mean"),
                tp=fmt(t, "p50"),
                t95=fmt(t, "p95"),
                fp=fmt(tot, "p50"),
                f95=fmt(tot, "p95"),
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
    md = outdir / "latest.md"
    md.write_text(
        f"# TASK-003 LLM benchmark — {report['generated_at']}\n\n"
        f"status: **{report['status']}**\n\n{render_table(report['endpoints'])}\n",
        encoding="utf-8",
    )
    paths.append(_display_path(md))
    return paths


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    env = load_env(Path(args.env_file))
    endpoints = resolve_endpoints(env)
    ready = [e for e in endpoints if e.api_key]

    results: list[dict[str, Any]] = []
    for ep in endpoints:
        if not ep.api_key:
            results.append(
                {
                    "name": ep.name,
                    "base_url": ep.base_url,
                    "model": ep.model,
                    "key_env": ep.key_env,
                    "status": MISSING_KEY,
                }
            )
            continue
        llm = make_llm(ep, timeout=args.timeout, trust_env=args.proxy)
        try:
            results.append(
                await bench_endpoint(ep, llm, runs=args.runs, interval=args.interval)
            )
        finally:
            await llm.close()

    status = STATUS_BLOCKED if not ready else STATUS_OK
    if ready and all(r["status"] == STATUS_ERROR for r in results if r["status"] != MISSING_KEY):
        status = STATUS_ERROR
    return {
        "task": "TASK-003",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runs_per_probe": args.runs,
        "status": status,
        "endpoints": results,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    p.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    p.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--interval", type=float, default=0.2)
    p.add_argument("--proxy", action="store_true", help="httpx trust_env=True 走系统代理")
    args = p.parse_args(argv)

    report = asyncio.run(_run(args))
    artifacts = write_artifacts(report, args.outdir)

    print(render_table(report["endpoints"]))
    print(f"\nstatus: {report['status']}")
    for a in artifacts:
        print(f"wrote {a}")
    if report["status"] == STATUS_BLOCKED:
        missing = [e["key_env"] for e in report["endpoints"] if e["status"] == MISSING_KEY]
        print(f"\nblocked: 零可用 key，缺失环境变量 {missing}；"
              "在 .env 填入任意一家后重跑。")
        return 2
    return 0 if report["status"] == STATUS_OK else 1


if __name__ == "__main__":
    raise SystemExit(main())
