"""memory/*.json fixture → 紧凑记忆摘要 + 触发词表，按需注入 agent input。

MVP 语义：fixture 是"派蒙已经记得的事"。模仿人类回忆——记忆是后台
资源而不是常驻 prompt：只有本轮用户输入命中触发词（地名/事件/回忆
词）时才把摘要放进输入，且命中后挂住 MEMORY_HOT_TURNS 轮供追问；
平时不注入，让模型专注当下。真实检索式记忆系统见
.loom/design/MEMORY_SYSTEM_DESIGN.md，本 loader 只解决"全量常驻过重"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

#: 命中后记忆摘要继续注入的轮数（话题上桌后追问不再要求关键词）。
MEMORY_HOT_TURNS = 3

#: 通用回忆触发词：用户在主动调取记忆，与具体地名无关。
GENERIC_TRIGGERS = (
    "记得",
    "记不记得",
    "上次",
    "之前",
    "以前",
    "那时候",
    "当时",
    "旅行",
    "去过",
    "回忆",
    "怀念",
)


@dataclass(frozen=True)
class MemoryPack:
    """装载结果：注入文本 + 触发词集合。"""

    text: str
    triggers: frozenset[str]


def load_memory(root: str | Path) -> MemoryPack:
    """装载目录下全部 *.json fixture → MemoryPack；无内容 → 空 pack。"""
    root = Path(root)
    if not root.is_dir():
        return MemoryPack("", frozenset())
    parts: list[str] = []
    triggers: set[str] = set(GENERIC_TRIGGERS)
    for f in sorted(root.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rendered, trigs = _render(data)
        if rendered:
            parts.append(rendered)
            triggers.update(trigs)
    return MemoryPack("｜".join(parts), frozenset(triggers))


def load_memory_digest(root: str | Path) -> str:
    """兼容旧签名：只要注入文本。"""
    return load_memory(root).text


def _render(data: object) -> tuple[str, set[str]]:
    if isinstance(data, dict) and isinstance(data.get("trip"), dict):
        return _render_trip(data["trip"])
    # 兜底：未知形状 fixture 压成一行 JSON（截断防爆 prompt）
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))[:800], set()


def _render_trip(t: dict) -> tuple[str, set[str]]:
    head = (
        f"共同记忆·{t.get('title', '')}"
        f"（{t.get('city', '')} {t.get('start_date', '')}至"
        f"{t.get('end_date', '')}，同行：{t.get('companion', '')}，"
        f"氛围：{t.get('mood', '')}）"
    )
    days = "；".join(
        f"第{d.get('day')}天{d.get('title', '')}：{d.get('summary', '')}"
        for d in t.get("days", [])
    )
    mems = "；".join(
        str(m.get("content", "")) for m in t.get("overall_memories", [])
    )
    text = f"{head}。行程：{days}。你记得的：{mems}"
    # 触发词：标题/城市/每日标题/事件地点——都是回忆锚点
    triggers = {
        str(t.get("title", "")),
        str(t.get("city", "")),
        *[str(d.get("title", "")) for d in t.get("days", [])],
        *[
            str(e.get("location", ""))
            for d in t.get("days", [])
            for e in d.get("events", [])
        ],
    }
    triggers.discard("")
    return text, triggers
