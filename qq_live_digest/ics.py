"""ICS / 日历导出（Roadmap A13）。

把「有明确时间的待办」导成 iCalendar（RFC 5545），供手机 / 桌面日历下载或订阅：

- **只读**：只读 `tasks` 表，不写库、不联网、不改任务；
- **不带走时区依赖**：时间以**浮动本地时间**写出（无 `TZID` / 无 `Z`），日历端按本机时区解释；
- 只有日期没有时刻的截止（本地 `00:00`）当作**全天事件**（`VALUE=DATE`）；
- 没有截止时间的待办**不导出**（避免制造假日程）。

纯字符串处理，没有 I/O，所以可以直接单测。
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterable

from . import taskstatus
from .timeutil import iso, now_local, parse_iso

PRODID = "-//qq-live-digest//QQ digest tasks//CN"
DEFAULT_CALENDAR_NAME = "QQ 群待办"
DEFAULT_DURATION_MINUTES = 60
MAX_LINE_OCTETS = 75
CATEGORY_LABEL = {"urgent": "紧急", "action": "待办", "academic": "学业", "info": "通知"}


def escape_text(value: Any) -> str:
    """RFC 5545 TEXT 转义：反斜杠、分号、逗号、换行。"""
    text = str(value if value is not None else "")
    text = text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def fold_line(line: str) -> str:
    """按 75 字节折行（续行以空格开头）。

    RFC 5545 的 75 字节上限**包含**续行前导的那个空格，所以从第二行起每行只放 74 字节；
    按字节切以免拆坏多字节字符。
    """
    if len(line.encode("utf-8")) <= MAX_LINE_OCTETS:
        return line
    segments: list[str] = []
    current = b""
    limit = MAX_LINE_OCTETS
    for char in line:
        chunk = char.encode("utf-8")
        if current and len(current) + len(chunk) > limit:
            segments.append(current.decode("utf-8"))
            current = b""
            limit = MAX_LINE_OCTETS - 1
        current += chunk
    if current:
        segments.append(current.decode("utf-8"))
    return "\r\n ".join(segments)


def is_all_day(moment: dt.datetime) -> bool:
    return (moment.hour, moment.minute, moment.second) == (0, 0, 0)


def _utc_stamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _local_stamp(moment: dt.datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%S")


def _date_stamp(day: dt.date) -> str:
    return day.strftime("%Y%m%d")


def _describe(task: dict[str, Any]) -> str:
    parts: list[str] = []
    action = str(task.get("action") or "").strip()
    if action:
        parts.append(f"要做：{action}")
    groups = [str(name) for name in (task.get("groups") or []) if str(name or "").strip()]
    if groups:
        parts.append("来自群：" + "、".join(groups))
    category = CATEGORY_LABEL.get(str(task.get("category") or ""), "")
    if category:
        parts.append(f"分类：{category}")
    return "\n".join(parts)


def build_events(
    tasks: Iterable[dict[str, Any]],
    *,
    now: dt.datetime | None = None,
    include_done: bool = False,
    duration_minutes: int = DEFAULT_DURATION_MINUTES,
) -> list[dict[str, Any]]:
    """把有截止时间的待办整理成事件（结构化，供测试与 API 复用）。"""
    stamp = now or now_local()
    duration = max(5, int(duration_minutes or DEFAULT_DURATION_MINUTES))
    events: list[dict[str, Any]] = []
    for task in tasks:
        if taskstatus.is_archived(task):
            continue
        done = taskstatus.is_done(task)
        if done and not include_done:
            continue
        moment = parse_iso(task.get("deadline"))
        if moment is None:
            continue
        task_id = int(task.get("id") or 0)
        all_day = is_all_day(moment)
        events.append(
            {
                "uid": f"qq-digest-task-{task_id}@qq-live-digest",
                "id": task_id,
                "summary": str(task.get("summary") or "").strip() or f"待办 #{task_id}",
                "description": _describe(task),
                "category": str(task.get("category") or "info"),
                "groups": [str(name) for name in (task.get("groups") or [])],
                "start": moment,
                "end": moment + dt.timedelta(minutes=duration),
                "all_day": all_day,
                "deadline": iso(moment),
                "done": done,
                "stamp": stamp,
            }
        )
    events.sort(key=lambda item: (item["start"], item["id"]))
    return events


def build_calendar(
    tasks: Iterable[dict[str, Any]],
    *,
    now: dt.datetime | None = None,
    include_done: bool = False,
    duration_minutes: int = DEFAULT_DURATION_MINUTES,
    calendar_name: str = DEFAULT_CALENDAR_NAME,
    prodid: str = PRODID,
) -> str:
    """生成完整 VCALENDAR 文本（CRLF 行尾，必要时折行）。"""
    events = build_events(
        tasks, now=now, include_done=include_done, duration_minutes=duration_minutes
    )
    stamp = events[0]["stamp"] if events else (now or now_local())
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{escape_text(prodid)}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape_text(calendar_name)}",
    ]
    for event in events:
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{escape_text(event['uid'])}")
        lines.append(f"DTSTAMP:{_utc_stamp(stamp)}")
        if event["all_day"]:
            day = event["start"].date()
            lines.append(f"DTSTART;VALUE=DATE:{_date_stamp(day)}")
            lines.append(f"DTEND;VALUE=DATE:{_date_stamp(day + dt.timedelta(days=1))}")
        else:
            lines.append(f"DTSTART:{_local_stamp(event['start'])}")
            lines.append(f"DTEND:{_local_stamp(event['end'])}")
        lines.append(f"SUMMARY:{escape_text(event['summary'])}")
        if event["description"]:
            lines.append(f"DESCRIPTION:{escape_text(event['description'])}")
        lines.append(f"CATEGORIES:{escape_text(CATEGORY_LABEL.get(event['category'], '通知'))}")
        lines.append(f"STATUS:{'COMPLETED' if event['done'] else 'CONFIRMED'}")
        lines.append("TRANSP:OPAQUE")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "".join(fold_line(line) + "\r\n" for line in lines)


def payload(
    tasks: Iterable[dict[str, Any]],
    *,
    now: dt.datetime | None = None,
    include_done: bool = False,
) -> dict[str, Any]:
    """给 CLI / API 的只读摘要（不含正文，只有计数与时间范围）。"""
    events = build_events(tasks, now=now, include_done=include_done)
    stamps = [event["deadline"] for event in events]
    return {
        "ok": True,
        "events": len(events),
        "all_day": sum(1 for event in events if event["all_day"]),
        "first": stamps[0] if stamps else "",
        "last": stamps[-1] if stamps else "",
    }


__all__ = [
    "DEFAULT_CALENDAR_NAME",
    "DEFAULT_DURATION_MINUTES",
    "PRODID",
    "build_calendar",
    "build_events",
    "escape_text",
    "fold_line",
    "is_all_day",
    "payload",
]
