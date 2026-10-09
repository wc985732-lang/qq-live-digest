"""待办状态口径（读接口共用）。

群通知转成的待办有 5 种状态（见 `store.TASK_STATUSES`）：`candidate` / `open` / `done` /
`dismissed` / `expired`。对外的读接口——待办台、REST、MCP、冲突检测（A12）、日历导出
（A13）——必须用同一套口径，否则「已忽略 / 已过期」的待办会重新冒出来（曾经就是这样）。

本模块只放纯判断，不碰数据库，便于各层复用与单测。
"""

from __future__ import annotations

from typing import Any, Mapping

DONE_STATUS = "done"
ARCHIVED_STATUSES = ("dismissed", "expired")
OPEN_STATUSES = ("candidate", "open")
TASK_STATUSES = frozenset(OPEN_STATUSES + (DONE_STATUS,) + ARCHIVED_STATUSES)


def is_done(task: Mapping[str, Any]) -> bool:
    """是否已完成：优先看 status，兼容只带 `done` 布尔的老调用。"""
    return str(task.get("status") or "") == DONE_STATUS or bool(task.get("done"))


def is_archived(task: Mapping[str, Any]) -> bool:
    """是否已归档（被忽略 / 已过期）——任何读接口都不该展示。"""
    return str(task.get("status") or "") in ARCHIVED_STATUSES


def is_closed(task: Mapping[str, Any]) -> bool:
    """已完成或已归档：总之不再是「活跃待办」。"""
    return is_done(task) or is_archived(task)


def statuses_for(include_done: bool) -> tuple[str, ...]:
    """活跃待办的状态集合；include_done=True 时附带 `done`（永不含归档态）。"""
    return OPEN_STATUSES + ((DONE_STATUS,) if include_done else ())


__all__ = [
    "ARCHIVED_STATUSES",
    "DONE_STATUS",
    "OPEN_STATUSES",
    "TASK_STATUSES",
    "is_archived",
    "is_closed",
    "is_done",
    "statuses_for",
]
