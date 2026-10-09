"""只读 MCP 接口（Roadmap A1）。

把 A15 的只读口径包成 MCP Tool，让 Cursor / Claude 这类 MCP 客户端能安全查询
「通知 / 待办 / 截止时间 / 搜索历史消息」。

- 只暴露 4 个**只读** Tool，没有任何写入、删除或发送能力；
- 传输按 MCP 的 stdio 约定：一行一条 JSON-RPC 2.0 消息，**stdout 只放协议消息**
  （日志请走 stderr / 文件，否则会污染协议流）；
- 与 A15 共用 `restapi.py` 的口径，不另起一套。

`handle_message()` 是纯函数（喂一条请求、还一条响应），所以协议层可以脱离进程直接单测。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Iterable

from . import restapi
from .timeutil import now_local

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
        "description": "按关键词搜历史群消息（只读；本地库，不含发送者 ID）。",
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
)


def list_tools() -> list[dict[str, Any]]:
    """Tool 定义（深拷贝，调用方改不动内部常量）。"""
    return [json.loads(json.dumps(tool, ensure_ascii=False)) for tool in TOOLS]


def _int_arg(args: dict[str, Any], name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(args.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


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


_HANDLERS: dict[str, Callable[[Any, dict[str, Any]], dict[str, Any]]] = {
    "recent_notifications": _recent_notifications,
    "todos": _todos,
    "deadlines": _deadlines,
    "search_messages": _search_messages,
}


def call_tool(store: Any, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """执行一个只读 Tool，返回 MCP 的 tools/call 结果结构。"""
    handler = _HANDLERS[str(name or "")]
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
                "instructions": "只读：通知 / 待办 / 截止时间 / 搜索历史消息；不写库、不发送 QQ。",
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
    """按 MCP stdio 约定逐行读 JSON-RPC、逐行写响应（只读）。"""
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

