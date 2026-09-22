"""MemoryProvider 协议 + mem0 实现的单元测试（client 注入假对象）。"""

from __future__ import annotations

import asyncio
import time

from memory.mem0_provider import Mem0MemoryProvider


class FakeMem0:
    def __init__(self, results=None, fail=False):
        self.results = results if results is not None else []
        self.fail = fail
        self.added = []
        self.searches = []

    def search(self, query, *, top_k, threshold, filters):
        self.searches.append((query, top_k, filters))
        if self.fail:
            raise RuntimeError("embed boom")
        return {"results": self.results}

    def add(self, messages, **kwargs):
        self.added.append((messages, kwargs))
        return {"results": []}


def _provider(client, tmp_path=None) -> Mem0MemoryProvider:
    return Mem0MemoryProvider(
        api_key="x",
        persist_dir=tmp_path or "data/test-mem0",
        client=client,
    )


async def test_recall_returns_joined_memories(tmp_path):
    client = FakeMem0(
        results=[
            {"memory": "用户在鸡鸣寺摔过一跤"},
            {"memory": "用户喜欢夜游秦淮河"},
            {"memory": ""},
        ]
    )
    p = _provider(client, tmp_path)
    out = await p.recall("南京怎么样")
    assert out == "用户在鸡鸣寺摔过一跤；用户喜欢夜游秦淮河"
    assert client.searches[0][2] == {"user_id": "local-user"}


async def test_recall_empty_query_short_circuits(tmp_path):
    client = FakeMem0()
    p = _provider(client, tmp_path)
    assert await p.recall("") is None
    assert client.searches == []


async def test_recall_failure_degrades_to_none(tmp_path):
    """检索挂了不挡对话：静默降级 None。"""
    p = _provider(FakeMem0(fail=True), tmp_path)
    assert await p.recall("南京") is None


def test_record_writes_background(tmp_path):
    client = FakeMem0()
    p = _provider(client, tmp_path)
    p.record("今天好累", "那就睡啊")
    for _ in range(50):
        if client.added:
            break
        time.sleep(0.02)
    assert client.added, "record 应后台写回"
    msgs, kwargs = client.added[0]
    assert msgs[0]["content"] == "今天好累"
    assert msgs[1]["content"] == "那就睡啊"
    assert kwargs["user_id"] == "local-user"


def test_record_skips_empty_sides(tmp_path):
    client = FakeMem0()
    p = _provider(client, tmp_path)
    p.record("", "没有用户输入不写")
    p.record("没有回复也不写", "")
    time.sleep(0.1)
    assert client.added == []


def test_seed_fixture_once(tmp_path):
    client = FakeMem0()
    p = _provider(client, tmp_path)
    (tmp_path / "mem").mkdir()
    (tmp_path / "mem" / "t.json").write_text(
        '{"trip": {"title": "测试行", "city": "测试城", "days": []}}',
        encoding="utf-8",
    )
    n = p.seed_fixture(tmp_path / "mem")
    assert n > 0 and client.added
    # infer=False：种子直存不走 LLM 抽取
    assert all(kw.get("infer") is False for _, kw in client.added)
    # 标记文件防重复种子
    assert p.seed_fixture(tmp_path / "mem") == 0
