"""记忆层：fixture 装载 + system prompt 记忆槽位注入 + mem0 provider。"""

from __future__ import annotations

import json
from pathlib import Path

from character.agent import CharacterAgent
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
    # 触发词是 pack 元数据（fixture triggers + 结构锚点），供种子/检索用
    assert "南京" in pack.triggers


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


def test_memory_always_in_agent_input():
    """常驻注入：记忆是 system prompt stable core 的一部分，
    不做机械门控——"什么时候用"由 prompt 规则管。"""
    core = ConversationCore(memory_text="共同记忆·测试")
    for text in ("你好", "南京好玩吗", ""):
        payload = core.context.build_agent_input(
            state=core.state, last_user_text=text
        )
        assert payload["memory"] == "共同记忆·测试"


def test_memory_lands_in_system_prompt_not_turn_input():
    """记忆渲染进 system 消息（与人格同层），不进本轮输入块。"""
    from providers.llm.base import LLMProvider

    class _LLM(LLMProvider):
        def stream_reply(self, messages, **_):
            yield ""
            return

        async def complete_structured(self, messages, **_):
            raise NotImplementedError

        async def close(self) -> None:
            pass

    core = ConversationCore(memory_text="共同记忆·测试")
    agent = CharacterAgent(_LLM())
    payload = core.context.build_agent_input(
        state=core.state, last_user_text="随便聊聊"
    )
    messages = agent.build_messages(payload)
    assert messages[0]["role"] == "system"
    assert "记忆：共同记忆·测试" in messages[0]["content"]
    # 本轮输入块里不再出现 memory 行
    assert "memory:" not in messages[-1]["content"]


def test_no_memory_no_slot():
    core = ConversationCore()
    payload = core.context.build_agent_input(state=core.state)
    assert payload["memory"] is None
