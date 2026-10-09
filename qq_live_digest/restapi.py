"""只读 REST API 的共享实现（Roadmap A15）。

网页待办台与 CLI 共用同一份「读什么、字段叫什么」的口径；本模块只做 **SELECT**，
不写库、不联网、不推送，也不处理 token —— 鉴权在传输层（`webapp.py`）完成。
这样 JSON 结构与 HTTP 传输解耦，纯函数部分可以直接单元测试。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from . import taskstatus
from .timeutil import iso, now_local, parse_iso

API_VERSION = "1"
SERVICE = "qq-tasks"
AUTH_HINT = "请求头 X-Token，或查询参数 ?token="

# 只读接口目录：path / method / 是否需要 token / 说明
ENDPOINTS = (
    {"path": "/health", "method": "GET", "auth": False, "desc": "存活探针（无需 token）"},
    {"path": "/api", "method": "GET", "auth": True, "desc": "接口目录与版本"},
    {"path": "/api/notifications", "method": "GET", "auth": True, "desc": "最近的群通知摘要（正文截断、脱敏）"},
    {"path": "/api/tasks", "method": "GET", "auth": True, "desc": "待办清单（今天 / 本周 / 以后 / 已完成）"},
    {"path": "/api/deadlines", "method": "GET", "auth": True, "desc": "带截止时间的待办，按时间升序"},
    {"path": "/api/calendar.ics", "method": "GET", "auth": True, "desc": "有明确时间的待办导出为 iCalendar（ICS）"},
    {"path": "/api/conflicts", "method": "GET", "auth": True, "desc": "潜在时间冲突：截止时间相近的待办按组返回"},
    {"path": "/api/panel", "method": "GET", "auth": True, "desc": "消息处理可观测面板（脱敏）"},
)


def clamp_int(value: Any, default: int, low: int, high: int) -> int:
    """把外部传入的整数限到 [low, high]；非法值用 default。HTTP / MCP / REST 共用一份。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _as_int(value: Any, default: int = 0) -> int:
    """宽松取整：脏数据不抛异常。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def health_payload() -> dict[str, Any]:
    return {"ok": True, "service": SERVICE, "api_version": API_VERSION, "read_only": True}


def index_payload() -> dict[str, Any]:
    """接口目录：给 A1（MCP）/ 脚本先自描述一遍「能读什么」。"""
    return {
        "ok": True,
        "service": SERVICE,
        "api_version": API_VERSION,
        "read_only": True,
        "auth": AUTH_HINT,
        "endpoints": [dict(item) for item in ENDPOINTS],
    }


def notifications(store: Any, *, limit: int = 8) -> dict[str, Any]:
    """最近的摘要（通知）：只给标题级信息，正文截断，不带发送者。"""
    count = clamp_int(limit or 8, 8, 1, 50)
    items: list[dict[str, Any]] = []
    for digest in store.recent_digests(limit=count):
        item_count = _as_int(digest.get("item_count"))
        items.append(
            {
                "id": digest.get("id"),
                "kind": digest.get("kind"),
                "created_at": digest.get("created_at"),
                "item_count": item_count,
                "summary": f"{item_count} 条重点",
                "body": str(digest.get("body") or "")[:600],
            }
        )
    return {"ok": True, "count": len(items), "items": items}


def deadlines(
    store: Any,
    *,
    now: dt.datetime | None = None,
    limit: int = 200,
    include_done: bool = False,
) -> dict[str, Any]:
    """带截止时间的待办，按截止时间升序；标出是否已逾期与剩余小时数。"""
    stamp = now or now_local()
    cap = clamp_int(limit or 200, 200, 1, 1000)
    rows: list[dict[str, Any]] = []
    for task in store.list_tasks(statuses=taskstatus.statuses_for(bool(include_done))):
        moment = parse_iso(task.get("deadline"))
        if moment is None:
            continue
        done = taskstatus.is_done(task)
        rows.append(
            {
                "id": _as_int(task.get("id")),
                "summary": str(task.get("summary") or ""),
                "action": str(task.get("action") or ""),
                "category": str(task.get("category") or "info"),
                "importance": _as_int(task.get("importance")),
                "status": str(task.get("status") or ""),
                "done": done,
                "deadline": iso(moment),
                "overdue": bool(not done and moment < stamp),
                "hours_left": round((moment - stamp).total_seconds() / 3600, 1),
                "groups": [str(name) for name in (task.get("groups") or [])],
            }
        )
    rows.sort(key=lambda item: (item["deadline"], item["id"]))
    return {
        "ok": True,
        "as_of": iso(stamp),
        "count": len(rows[:cap]),
        "total": len(rows),
        "overdue": sum(1 for item in rows if item["overdue"]),
        "items": rows[:cap],
    }


def todos(
    store: Any,
    *,
    now: dt.datetime | None = None,
    include_done: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    """待办清单（只读）：未完成在前、逾期优先、越重要越靠前。"""
    stamp = now or now_local()
    cap = clamp_int(limit or 200, 200, 1, 1000)
    tasks = store.list_tasks(statuses=taskstatus.statuses_for(bool(include_done)))
    items: list[dict[str, Any]] = []
    for task in tasks:
        moment = parse_iso(task.get("deadline"))
        done = taskstatus.is_done(task)
        items.append(
            {
                "id": _as_int(task.get("id")),
                "summary": str(task.get("summary") or ""),
                "action": str(task.get("action") or ""),
                "category": str(task.get("category") or "info"),
                "importance": _as_int(task.get("importance")),
                "status": str(task.get("status") or ""),
                "done": done,
                "candidate": str(task.get("status") or "") == "candidate",
                "deadline": iso(moment) if moment else "",
                "overdue": bool(moment and not done and moment < stamp),
                "groups": [str(name) for name in (task.get("groups") or [])],
            }
        )
    items.sort(
        key=lambda item: (
            item["done"],
            0 if item["overdue"] else 1,
            -item["importance"],
            item["deadline"] or "9999",
            item["id"],
        )
    )
    return {
        "ok": True,
        "as_of": iso(stamp),
        "count": len(items[:cap]),
        "total": len(items),
        "overdue": sum(1 for item in items if item["overdue"]),
        "items": items[:cap],
    }
