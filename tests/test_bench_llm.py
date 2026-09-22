"""TASK-003 acceptance 2：bench_llm 对未配置 key 的 endpoint 标 MISSING_KEY 并跳过；
以及 benchmark 统计/落盘路径的离线验证（httpx.MockTransport，不走真实网络）。

verify_by: pytest tests/test_bench_llm.py 通过。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "bench_llm", ROOT / "scripts" / "bench_llm.py"
)
bench_llm = importlib.util.module_from_spec(_spec)
sys.modules["bench_llm"] = bench_llm
_spec.loader.exec_module(bench_llm)


def sse_body(tokens: list[str]) -> bytes:
    lines = [f"data: {json.dumps({'choices': [{'delta': {'content': t}}]})}" for t in tokens]
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


REPLY_JSON = json.dumps(
    {"speech": "哈？", "emotion": "teasing", "energy": 0.8, "should_continue": False}
)


def mock_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body.get("response_format"):
        return httpx.Response(200, content=sse_body([REPLY_JSON]))
    return httpx.Response(200, content=sse_body(["哈？", "真的假的？"]))


def make_endpoint(env: dict[str, str], name: str) -> bench_llm.Endpoint:
    return next(e for e in bench_llm.resolve_endpoints(env) if e.name == name)


def mock_llm(endpoint: bench_llm.Endpoint) -> bench_llm.OpenAICompatibleLLM:
    return bench_llm.OpenAICompatibleLLM(
        base_url=endpoint.base_url,
        api_key=endpoint.api_key or "",
        model=endpoint.model,
        transport=httpx.MockTransport(mock_handler),
    )


def test_resolve_endpoints_marks_missing_key():
    eps = bench_llm.resolve_endpoints({})
    assert {e.name for e in eps} == {"deepseek", "qwen"}
    assert all(e.api_key is None and e.status == "MISSING_KEY" for e in eps)

    env = {"DEEPSEEK_API_KEY": "  sk-x  ", "QWEN_MODEL": "qwen-plus"}
    eps = bench_llm.resolve_endpoints(env)
    ds = next(e for e in eps if e.name == "deepseek")
    qw = next(e for e in eps if e.name == "qwen")
    assert ds.api_key == "sk-x" and ds.status == "ok"
    assert qw.status == "MISSING_KEY" and qw.model == "qwen-plus"


def test_percentile_and_summarize():
    vals = list(range(10, 201, 10))  # 10..200, n=20
    assert bench_llm.percentile(vals, 0.5) == pytest.approx(105.0)
    assert bench_llm.percentile(vals, 0.95) == pytest.approx(190.5)
    assert bench_llm.percentile([42.0], 0.95) == 42.0

    s = bench_llm.summarize(vals)
    assert s["n"] == 20 and s["mean"] == 105.0 and s["p95"] == 190.5
    assert bench_llm.summarize([]) == {"n": 0}


async def test_bench_endpoint_collects_ttft_and_structured():
    ep = make_endpoint({"DEEPSEEK_API_KEY": "sk-x"}, "deepseek")
    result = await bench_llm.bench_endpoint(ep, mock_llm(ep), runs=5)

    assert result["status"] == "ok"
    assert result["ttft_ms"]["n"] == 5
    assert len(result["samples"]) == 5
    assert result["structured"] == {
        "attempts": 5,
        "success": 5,
        "parse_errors": 0,
        "transport_errors": 0,
        "rate": 1.0,
    }
    assert "warmup_ttft_ms" in result


async def test_bench_endpoint_counts_parse_failures():
    def bad_json_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("response_format"):
            return httpx.Response(200, content=sse_body(["不是", "JSON"]))
        return httpx.Response(200, content=sse_body(["嗯"]))

    ep = make_endpoint({"DEEPSEEK_API_KEY": "sk-x"}, "deepseek")
    llm = bench_llm.OpenAICompatibleLLM(
        base_url=ep.base_url,
        api_key="sk-x",
        model=ep.model,
        transport=httpx.MockTransport(bad_json_handler),
    )
    result = await bench_llm.bench_endpoint(ep, llm, runs=4)

    assert result["structured"]["attempts"] == 4
    assert result["structured"]["success"] == 0
    assert result["structured"]["parse_errors"] == 4
    assert result["structured"]["rate"] == 0.0
    assert result["ttft_ms"]["n"] == 4


def test_main_zero_keys_blocked(tmp_path, monkeypatch):
    for key in ("DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DASHSCOPE_API_KEY=\nDEEPSEEK_API_KEY=\n", encoding="utf-8"
    )
    rc = bench_llm.main(
        ["--env-file", str(env_file), "--outdir", str(tmp_path / "bench")]
    )
    assert rc == 2

    report = json.loads(
        (tmp_path / "bench" / "latest.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "blocked"
    assert all(e["status"] == "MISSING_KEY" for e in report["endpoints"])
    assert (tmp_path / "bench" / "latest.md").exists()


def test_main_partial_keys_runs_and_skips(tmp_path, monkeypatch):
    for key in ("DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        bench_llm,
        "make_llm",
        lambda ep, *, timeout, trust_env: mock_llm(ep),
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEEPSEEK_API_KEY=sk-x\nDASHSCOPE_API_KEY=\n", encoding="utf-8"
    )
    rc = bench_llm.main(
        [
            "--env-file",
            str(env_file),
            "--outdir",
            str(tmp_path / "bench"),
            "--runs",
            "3",
            "--interval",
            "0",
        ]
    )
    assert rc == 0

    report = json.loads(
        (tmp_path / "bench" / "latest.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "ok"
    by_name = {e["name"]: e for e in report["endpoints"]}
    assert by_name["qwen"]["status"] == "MISSING_KEY"
    assert by_name["deepseek"]["status"] == "ok"
    assert by_name["deepseek"]["ttft_ms"]["n"] == 3
    assert by_name["deepseek"]["structured"]["rate"] == 1.0
