"""MCP 接口（Roadmap A1 只读 + A2 可写待办）。

把 A15 的口径包成 MCP Tool，让 Cursor / Claude 这类 MCP 客户端能安全查询，并在**明确确认**后
有限地改动**待办**：

- 只读 Tool：`recent_notifications` / `todos` / `deadlines` / `search_messages` / `recent_audit`；
- 可写 Tool（仅限 `tasks` 表）：`set_task_done` / `add_task`，**必须 `confirm=true`**，
  每次调用（含被拒）都写 `audit_log` 审计；
- **没有任何 QQ 发送 / 删群 / 改设置的入口**，写操作只碰 `tasks`；
- 传输按 MCP 的 stdio 约定：一行一条 JSON-RPC 2.0 消息，**stdout 只放协议消息**
  （日志请走 stderr / 文件，否则会污染协议流）；
- 与 A15 共用 `restapi.py` 的口径，不另起一套。

`handle_message()` 是纯函数（喂一条请求、还一条响应），所以协议层可以脱离进程直接单测。
"""

from __future__ import annotations

import hashlib
import json
import sys
from typing import Any, Callable, Iterable

from . import restapi
from .timeutil import now_local, parse_iso

SERVER_NAME = "qq-live-digest"
SERVER_VERSION = "1.0"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "recent_notifications",
        "description": "最近的群通知摘要（只读；正文截断到 600 字、不含发送者）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "todos",
        "description": "待办清单（只读；未完成在前、逾期优先、越重要越靠前）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_done": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "deadlines",
        "description": "带截止时间的待办，按截止时间升序，并标出是否逾期（只读）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_done": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "search_messages",
        "description": "按关键词搜历史群消息（只读；本地库，只给发送者显示名，不含 QQ 号）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要搜的关键词"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
                "hours": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 8760,
                    "default": 0,
                    "description": "只搜最近多少小时，0=不限",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "recent_audit",
        "description": "最近的写操作审计日志（只读；新→旧，含成功与被拒）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20}
            },
            "additionalProperties": False,
        },
    },
)


# A2 可写 Tool：只碰 tasks 表，必须 confirm=true，逐次写审计。其它一律不许。
WRITE_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "set_task_done",
        "description": "把一条待办标记为完成 / 重新打开（写操作：仅 tasks、必须 confirm=true、记审计）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "minimum": 1},
                "done": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "default": False, "description": "必须显式传 true 才会真正写入"},
            },
            "required": ["task_id", "confirm"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add_task",
        "description": "新建一条待办（写操作：仅 tasks、必须 confirm=true、记审计）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "待办标题"},
                "action": {"type": "string", "description": "需要做什么，可留空"},
                "deadline": {"type": "string", "description": "YYYY-MM-DD 或 YYYY-MM-DD HH:MM，可留空"},
                "confirm": {"type": "boolean", "default": False, "description": "必须显式传 true 才会真正写入"},
            },
            "required": ["summary", "confirm"],
            "additionalProperties": False,
        },
    },
)


def _annotate(tools: tuple[dict[str, Any], ...], *, read_only: bool) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for tool in tools:
        item = json.loads(json.dumps(tool, ensure_ascii=False))
        item["annotations"] = {"readOnlyHint": read_only, "destructiveHint": False}
        items.append(item)
    return items


def read_tools() -> list[dict[str, Any]]:
    """只读 Tool 定义（深拷贝，调用方改不动内部常量）。"""
    return _annotate(TOOLS, read_only=True)


def write_tools() -> list[dict[str, Any]]:
    """可写 Tool 定义（仅 tasks，必须 confirm）。"""
    return _annotate(WRITE_TOOLS, read_only=False)


def list_tools() -> list[dict[str, Any]]:
    """全部 Tool 定义：只读在前、可写在后。"""
    return read_tools() + write_tools()


def _int_arg(args: dict[str, Any], name: str, default: int, low: int, high: int) -> int:
    """读一个受限整数参数；限幅逻辑与 HTTP 层共用一份（restapi.clamp_int）。"""
    return restapi.clamp_int(args.get(name, default), default, low, high)


def _bool_arg(args: dict[str, Any], name: str, default: bool = False) -> bool:
    value = args.get(name, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _recent_notifications(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    return restapi.notifications(store, limit=_int_arg(args, "limit", 8, 1, 50))


def _todos(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    return restapi.todos(
        store,
        now=now_local(),
        include_done=_bool_arg(args, "include_done"),
        limit=_int_arg(args, "limit", 200, 1, 1000),
    )


def _deadlines(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    return restapi.deadlines(
        store,
        now=now_local(),
        include_done=_bool_arg(args, "include_done"),
        limit=_int_arg(args, "limit", 200, 1, 1000),
    )


def _search_messages(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query 不能为空")
    rows = store.search_messages(
        query,
        limit=_int_arg(args, "limit", 20, 1, 200),
        hours=_int_arg(args, "hours", 0, 0, 8760),
    )
    items = [
        {
            "msg_id": str(row.get("msg_id") or ""),
            "group": str(row.get("group_name") or row.get("group_id") or ""),
            "sender": str(row.get("sender_name") or ""),
            "ts": str(row.get("ts") or row.get("received_at") or ""),
            "text": str(row.get("source_text") or row.get("content") or "")[:500],
        }
        for row in rows
    ]
    return {"ok": True, "query": query, "count": len(items), "items": items}


def _recent_audit(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    rows = store.recent_audit(limit=_int_arg(args, "limit", 20, 1, 200))
    return {"ok": True, "count": len(rows), "items": rows}


def _needs_confirm(store: Any, tool: str, target: str, args: dict[str, Any]) -> None:
    """写操作闸门：没有显式 confirm=true 就拒绝，并记一条审计。"""
    if not _bool_arg(args, "confirm"):
        store.record_audit(tool=tool, target=target, ok=False, detail="缺少 confirm=true")
        raise ValueError("写操作必须显式 confirm=true")


def _set_task_done(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    task_id = _int_arg(args, "task_id", 0, 0, 2**31 - 1)
    done = _bool_arg(args, "done", True)
    target = f"task:{task_id}"
    if task_id <= 0:
        store.record_audit(tool="set_task_done", target=target, ok=False, detail="task_id 无效")
        raise ValueError("task_id 必须是正整数")
    _needs_confirm(store, "set_task_done", target, args)
    task = store.get_task(task_id)
    if task is None:
        store.record_audit(tool="set_task_done", target=target, ok=False, detail="任务不存在")
        raise ValueError(f"任务 {task_id} 不存在")
    changed = bool(store.set_task_status(task_id, bool(done)))
    store.record_audit(
        tool="set_task_done",
        target=target,
        ok=True,
        detail=("完成" if done else "重新打开") + ("" if changed else "（无变化）"),
    )
    return {
        "ok": True,
        "task_id": task_id,
        "done": bool(done),
        "changed": changed,
        "summary": str(task.get("summary") or ""),
    }


_CATEGORIES = ("urgent", "action", "academic", "info")


def _add_task(store: Any, args: dict[str, Any]) -> dict[str, Any]:
    summary = str(args.get("summary") or "").strip()
    if not summary:
        store.record_audit(tool="add_task", target="task:new", ok=False, detail="summary 为空")
        raise ValueError("summary 不能为空")
    deadline = str(args.get("deadline") or "").strip()
    if deadline and parse_iso(deadline) is None:
        store.record_audit(tool="add_task", target="task:new", ok=False, detail="deadline 格式不对")
        raise ValueError("deadline 必须是 YYYY-MM-DD 或 YYYY-MM-DD HH:MM")
    _needs_confirm(store, "add_task", "task:new", args)
    category = str(args.get("category") or "action")
    if category not in _CATEGORIES:
        category = "action"
    task_key = "mcp:" + hashlib.sha1(f"{summary}|{deadline}".encode("utf-8")).hexdigest()[:16]
    task_id = store.upsert_task(
        task_key=task_key,
        summary=summary,
        action=str(args.get("action") or ""),
        category=category,
        deadline=deadline,
        status="open",
        source="mcp",
    )
    store.record_audit(tool="add_task", target=f"task:{task_id}", ok=True, detail=summary)
    return {"ok": True, "task_id": int(task_id), "summary": summary, "deadline": deadline}


_HANDLERS: dict[str, Callable[[Any, dict[str, Any]], dict[str, Any]]] = {
    "recent_notifications": _recent_notifications,
    "todos": _todos,
    "deadlines": _deadlines,
    "search_messages": _search_messages,
    "recent_audit": _recent_audit,
    "set_task_done": _set_task_done,
    "add_task": _add_task,
}


def call_tool(store: Any, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """执行一个 Tool（只读或可写），返回 MCP 的 tools/call 结果结构。"""
    handler = _HANDLERS.get(str(name or ""))
    if handler is None:
        return _tool_result({"ok": False, "error": f"未知 Tool: {name or '(空)'}"}, is_error=True)
    try:
        payload = handler(store, dict(arguments or {}))
    except ValueError as error:
        return _tool_result({"ok": False, "error": str(error)}, is_error=True)
    return _tool_result(payload)


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_message(store: Any, message: Any) -> dict[str, Any] | None:
    """处理一条 JSON-RPC 消息；返回要发回的响应，通知类消息返回 None。"""
    if not isinstance(message, dict):
        return _error(None, -32600, "invalid request")
    method = str(message.get("method") or "")
    request_id = message.get("id")
    if request_id is None and method.startswith("notifications/"):
        return None
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if method == "initialize":
        version = str(params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION)
        return _result(
            request_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": "查询通知 / 待办 / 截止 / 历史消息与审计；可写仅限待办（需 confirm=true，逐次记审计）；不发送 QQ。",
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": list_tools()})
    if method == "tools/call":
        name = str(params.get("name") or "")
        if name not in _HANDLERS:
            return _error(request_id, -32602, f"未知 Tool: {name or '(空)'}")
        return _result(request_id, call_tool(store, name, params.get("arguments") or {}))
    return _error(request_id, -32601, f"method not found: {method}")


def serve_stdio(store: Any, *, stdin: Iterable[str] | None = None, stdout: Any = None) -> int:
    """按 MCP stdio 约定逐行读 JSON-RPC、逐行写响应（含可写 Tool）。"""
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    for raw in source:
        line = str(raw).strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            sink.write(json.dumps(_error(None, -32700, "parse error"), ensure_ascii=False) + "\n")
            sink.flush()
            continue
        try:
            response = handle_message(store, message)
        except Exception as error:  # noqa: BLE001 - 单个请求失败不该拖垮整个会话
            request_id = message.get("id") if isinstance(message, dict) else None
            response = _error(request_id, -32603, str(error))
        if response is None:
            continue
        sink.write(json.dumps(response, ensure_ascii=False) + "\n")
        sink.flush()
    return 0
