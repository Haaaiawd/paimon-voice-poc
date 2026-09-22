"""记忆 fixture 注入：loader 渲染 + agent input/prompt 透传。"""

from __future__ import annotations

import json
from pathlib import Path

from character.prompt import render_turn_input
from conversation import ConversationCore
from memory.loader import load_memory_digest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "memory"


def test_repo_fixture_loads_as_digest():
    digest = load_memory_digest(FIXTURE_DIR)
    assert "南京七日" in digest
    assert "第1天" in digest and "第7天" in digest
    # overall_memories 蒸馏条目也进摘要
    assert "梧桐" in digest
    # 紧凑：单摘要行级别，不是整本 JSON
    assert len(digest) < 3000


def test_missing_dir_returns_empty(tmp_path):
    assert load_memory_digest(tmp_path / "nope") == ""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert load_memory_digest(empty) == ""


def test_malformed_json_skipped(tmp_path):
    (tmp_path / "bad.json").write_text("{oops", encoding="utf-8")
    (tmp_path / "good.json").write_text(
        json.dumps({"trip": {"title": "测试行", "days": []}}, ensure_ascii=False),
        encoding="utf-8",
    )
    digest = load_memory_digest(tmp_path)
    assert "测试行" in digest


def test_memory_flows_into_turn_input():
    core = ConversationCore(memory_text="共同记忆·测试")
    payload = core.context.build_agent_input(
        state=core.state, last_user_text="记得南京吗"
    )
    assert payload["memory"] == "共同记忆·测试"
    rendered = render_turn_input(payload)
    assert 'memory: "共同记忆·测试"' in rendered
    # memory 行在 user 行之前
    assert rendered.index("memory:") < rendered.index('user:')


def test_no_memory_no_line():
    core = ConversationCore()
    payload = core.context.build_agent_input(state=core.state)
    assert payload["memory"] is None
    assert "memory:" not in render_turn_input(payload)
