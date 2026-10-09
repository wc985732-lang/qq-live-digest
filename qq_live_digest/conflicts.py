"""时间冲突检测（Roadmap A12）。

我们只有「一个统一的时间字段」——待办的 `deadline`（原始消息里的 start/end 没有被结构化）。
所以 A12 的口径是：**把截止时间落在同一时间窗（默认 30 分钟）内的多个待办当作潜在冲突**，
只做提醒，**不擅自改任务**（不改时间、不合并不删除）。

- 只读：只读 `tasks` 表，不写库、不联网；
- 已完成待办默认不参与；
- 没有截止时间的待办不参与；
- 分组按时间升序，同一组内首尾跨度不超过窗口（链式相邻不会滚成一大坨）。

纯函数，可直接单测。
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterable

from . import taskstatus
from .timeutil import iso, now_local, parse_iso

DEFAULT_WINDOW_MINUTES = 30


def _item(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(task.get("id") or 0),
        "summary": str(task.get("summary") or ""),
        "action": str(task.get("action") or ""),
        "category": str(task.get("category") or "info"),
        "deadline": iso(parse_iso(task.get("deadline"))),
        "groups": [str(name) for name in (task.get("groups") or [])],
        "done": taskstatus.is_done(task),
    }


def conflict_groups(
    tasks: Iterable[dict[str, Any]],
    *,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    include_done: bool = False,
) -> list[dict[str, Any]]:
    """返回潜在时间冲突分组（每组 >= 2 条），按开始时间升序。"""
    window = max(0, int(window_minutes if window_minutes is not None else DEFAULT_WINDOW_MINUTES))
    dated: list[tuple[dt.datetime, dict[str, Any]]] = []
    for task in tasks:
        if taskstatus.is_archived(task):
            continue
        if taskstatus.is_done(task) and not include_done:
            continue
        moment = parse_iso(task.get("deadline"))
        if moment is None:
            continue
        dated.append((moment, task))
    dated.sort(key=lambda pair: (pair[0], int(pair[1].get("id") or 0)))

    groups: list[list[tuple[dt.datetime, dict[str, Any]]]] = []
    current: list[tuple[dt.datetime, dict[str, Any]]] = []
    for moment, task in dated:
        if current and (moment - current[0][0]).total_seconds() > window * 60:
            groups.append(current)
            current = []
        current.append((moment, task))
    if current:
        groups.append(current)

    conflicts: list[dict[str, Any]] = []
    for group in groups:
        if len(group) < 2:
            continue
        starts = [moment for moment, _ in group]
        conflicts.append(
            {
                "start": iso(starts[0]),
                "end": iso(starts[-1]),
                "count": len(group),
                "span_minutes": round((starts[-1] - starts[0]).total_seconds() / 60.0, 1),
                "items": [_item(task) for _, task in group],
            }
        )
    conflicts.sort(key=lambda entry: (entry["start"], -entry["count"]))
    return conflicts


def payload(
    tasks: Iterable[dict[str, Any]],
    *,
    now: dt.datetime | None = None,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    include_done: bool = False,
) -> dict[str, Any]:
    """只读冲突报告：给 CLI / API 共用。"""
    stamp = now or now_local()
    window = max(0, int(window_minutes if window_minutes is not None else DEFAULT_WINDOW_MINUTES))
    conflicts = conflict_groups(tasks, window_minutes=window, include_done=include_done)
    return {
        "ok": True,
        "as_of": iso(stamp),
        "window_minutes": window,
        "count": len(conflicts),
        "tasks": sum(entry["count"] for entry in conflicts),
        "conflicts": conflicts,
    }


def render(report: dict[str, Any]) -> str:
    """把报告渲染成终端可读文本。"""
    lines = [
        f"时间冲突检测（窗口 {report.get('window_minutes')} 分钟）："
        f"{report.get('count', 0)} 组 / 涉及 {report.get('tasks', 0)} 条待办"
    ]
    if not report.get("count"):
        lines.append("没有发现时间相近的待办。")
        return "\n".join(lines)
    for index, entry in enumerate(report.get("conflicts") or [], start=1):
        lines.append(
            f"\n[{index}] {entry.get('start')} → {entry.get('end')}"
            f"（{entry.get('count')} 条，跨度 {entry.get('span_minutes')} 分钟）"
        )
        for item in entry.get("items") or []:
            groups = "、".join(item.get("groups") or [])
            suffix = f"（{groups}）" if groups else ""
            lines.append(f"  - #{item.get('id')} {item.get('summary')} @ {item.get('deadline')}{suffix}")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_WINDOW_MINUTES",
    "conflict_groups",
    "payload",
    "render",
]
