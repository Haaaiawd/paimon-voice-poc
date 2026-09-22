"""记忆 fixture 注入：loader 渲染 + 触发式门控 + prompt 透传。"""

from __future__ import annotations

import json
from pathlib import Path

from character.prompt import render_turn_input
from conversation import ConversationCore
from memory.loader import load_memory

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "memory"


def test_repo_fixture_loads_as_pack():
    pack = load_memory(FIXTURE_DIR)
    assert "南京七日" in pack.text
    assert "第1天" in pack.text and "第7天" in pack.text
    assert "梧桐" in pack.text
    assert len(pack.text) < 3000
    # 触发词：结构锚点（城市/日标题/事件地点）+ fixture 自声明 triggers
    assert "南京" in pack.triggers
    assert "鸡鸣寺" in pack.triggers
    assert "旅行" in pack.triggers


def test_missing_dir_returns_empty(tmp_path):
    pack = load_memory(tmp_path / "nope")
    assert pack.text == "" and not pack.triggers


def test_malformed_json_skipped(tmp_path):
    (tmp_path / "bad.json").write_text("{oops", encoding="utf-8")
    (tmp_path / "good.json").write_text(
        json.dumps({"trip": {"title": "测试行", "days": []}}, ensure_ascii=False),
        encoding="utf-8",
    )
    pack = load_memory(tmp_path)
    assert "测试行" in pack.text


def test_memory_gated_by_trigger():
    """不命中触发词不下发；命中后挂住几轮供追问。"""
    core = ConversationCore(
        memory_text="共同记忆·测试", memory_triggers=frozenset({"南京"})
    )
    ctx = core.context
    # 无关输入：不下发
    assert (
        ctx.build_agent_input(state=core.state, last_user_text="你好")["memory"]
        is None
    )
    # 命中：下发且挂住
    hit = ctx.build_agent_input(state=core.state, last_user_text="南京好玩吗")
    assert hit["memory"] == "共同记忆·测试"
    # 追问不带关键词也继续下发（hot 窗口 3 轮）
    for _ in range(2):
        follow = ctx.build_agent_input(
            state=core.state, last_user_text="那吃的呢"
        )
        assert follow["memory"] == "共同记忆·测试"
    # 窗口耗尽：回到不下发
    cold = ctx.build_agent_input(state=core.state, last_user_text="换个话题")
    assert cold["memory"] is None


def test_memory_renders_in_turn_input_before_user():
    core = ConversationCore(
        memory_text="共同记忆·测试", memory_triggers=frozenset({"南京"})
    )
    payload = core.context.build_agent_input(
        state=core.state, last_user_text="还记得南京吗"
    )
    rendered = render_turn_input(payload)
    assert 'memory: "共同记忆·测试"' in rendered
    assert rendered.index("memory:") < rendered.index('user:')


def test_no_memory_no_line():
    core = ConversationCore()
    payload = core.context.build_agent_input(state=core.state)
    assert payload["memory"] is None
    assert "memory:" not in render_turn_input(payload)
