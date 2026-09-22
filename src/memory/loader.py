"""memory/*.json fixture → 紧凑记忆摘要，注入每轮 agent input。

MVP 语义：fixture 是"派蒙已经记得的事"——启动时装载、逐轮随输入下发，
不做检索/裁剪（真实记忆系统是后续 capability，见 .loom/design/
MEMORY_SYSTEM_DESIGN.md）。目录不存在或为空 → 空串，prompt 不渲染该行。

支持 fixture 形状：{"trip": {...days[], overall_memories[]}}；
其余 JSON 以紧凑原样摘要兜底。
"""

from __future__ import annotations

import json
from pathlib import Path


def load_memory_digest(root: str | Path) -> str:
    """装载目录下全部 *.json fixture → 单行记忆摘要；无可读内容返回 ""。"""
    root = Path(root)
    if not root.is_dir():
        return ""
    parts: list[str] = []
    for f in sorted(root.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rendered = _render(data)
        if rendered:
            parts.append(rendered)
    return "｜".join(parts)


def _render(data: object) -> str:
    if isinstance(data, dict) and isinstance(data.get("trip"), dict):
        return _render_trip(data["trip"])
    # 兜底：未知形状 fixture 压成一行 JSON（截断防爆 prompt）
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))[:800]


def _render_trip(t: dict) -> str:
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
    return f"{head}。行程：{days}。你记得的：{mems}"
