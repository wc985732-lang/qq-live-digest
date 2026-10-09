"""只读 MCP 接口（Roadmap A1）回归测试。

守住：只暴露 4 个只读 Tool、没有写入/发送能力；call_tool 逐个可用；
JSON-RPC 分派（initialize/ping/tools/list/tools/call/未知方法/坏请求）与
stdio 解析错误都按协议返回正确错误码。
"""

from __future__ import annotations

import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest import mcp  # noqa: E402
from qq_live_digest.store import Store  # noqa: E402
from qq_live_digest.timeutil import iso  # noqa: E402

NOW = dt.datetime(2026, 9, 30, 9, 0, 0)


class ToolCatalogTest(unittest.TestCase):
    def test_lists_four_read_only_tools(self) -> None:
        tools = mcp.list_tools()
        names = {tool["name"] for tool in tools}
        self.assertEqual(
            names, {"recent_notifications", "todos", "deadlines", "search_messages"}
        )
        self.assertEqual(len(tools), 4)
        for tool in tools:
            self.assertIn("description", tool)
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_no_write_or_send_tools(self) -> None:
        names = {tool["name"] for tool in mcp.list_tools()}
        for forbidden in ("send", "send_message", "write", "create_task", "delete"):
            self.assertNotIn(forbidden, names)

    def test_search_messages_requires_query(self) -> None:
        tool = next(t for t in mcp.list_tools() if t["name"] == "search_messages")
        self.assertEqual(tool["inputSchema"]["required"], ["query"])

    def test_list_tools_returns_independent_copies(self) -> None:
        first = mcp.list_tools()
        first[0]["name"] = "tampered"
        self.assertNotEqual(mcp.list_tools()[0]["name"], "tampered")


class CallToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "mcp.sqlite3")

    def test_recent_notifications_on_empty_store(self) -> None:
        result = mcp.call_tool(self.store, "recent_notifications", {"limit": 5})
        self.assertFalse(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 0)

    def test_recent_notifications_reads_digests(self) -> None:
        self.store.insert_digest(
            kind="window",
            window_start=iso(NOW - dt.timedelta(minutes=10)),
            window_end=iso(NOW),
            body="x" * 900,
            payload=[{"summary": "a"}, {"summary": "b"}],
            message_count=5,
            llm_used=False,
        )
        result = mcp.call_tool(self.store, "recent_notifications", {"limit": 8})
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["count"], 1)
        self.assertEqual(len(payload["items"][0]["body"]), 600)

    def test_todos_flags_overdue_and_excludes_done(self) -> None:
        self.store.upsert_task(
            task_key="late",
            summary="已经过了的",
            category="action",
            deadline=iso(NOW - dt.timedelta(hours=2)),
        )
        done_id = self.store.upsert_task(
            task_key="done",
            summary="已完成的",
            category="action",
            deadline=iso(NOW + dt.timedelta(hours=1)),
        )
        self.store.set_task_status(done_id, True)
        payload = json.loads(mcp.call_tool(self.store, "todos", {})["content"][0]["text"])
        self.assertEqual(payload["count"], 1)
        self.assertTrue(payload["items"][0]["overdue"])
        with_done = json.loads(
            mcp.call_tool(self.store, "todos", {"include_done": True})["content"][0]["text"]
        )
        self.assertEqual(with_done["count"], 2)

    def test_deadlines_delegates_to_read_only_view(self) -> None:
        self.store.upsert_task(
            task_key="soon",
            summary="马上到的",
            category="action",
            deadline=iso(NOW + dt.timedelta(hours=3)),
        )
        payload = json.loads(mcp.call_tool(self.store, "deadlines", {})["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["items"][0]["summary"], "马上到的")

    def test_search_messages_finds_inserted_message(self) -> None:
        self.store.insert_message(
            msg_id="m1",
            group_id="g1",
            group_name="学院通知群",
            sender_name="张三",
            content="周三下午三点开组会，记得带材料",
            ts=iso(NOW),
            received_at=iso(NOW),
        )
        payload = json.loads(
            mcp.call_tool(self.store, "search_messages", {"query": "组会"})["content"][0]["text"]
        )
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["group"], "学院通知群")
        self.assertIn("组会", payload["items"][0]["text"])

    def test_search_messages_empty_query_is_error(self) -> None:
        result = mcp.call_tool(self.store, "search_messages", {"query": "   "})
        self.assertTrue(result["isError"])
        self.assertFalse(json.loads(result["content"][0]["text"])["ok"])


class HandleMessageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "rpc.sqlite3")

    def test_initialize_returns_server_info(self) -> None:
        response = mcp.handle_message(
            self.store,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        )
        self.assertEqual(response["id"], 1)
        result = response["result"]
        self.assertEqual(result["serverInfo"]["name"], mcp.SERVER_NAME)
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertIn("tools", result["capabilities"])

    def test_ping_returns_empty_result(self) -> None:
        response = mcp.handle_message(self.store, {"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(response["result"], {})

    def test_tools_list_returns_catalog(self) -> None:
        response = mcp.handle_message(self.store, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(len(names), 4)

    def test_tools_call_returns_text_content(self) -> None:
        response = mcp.handle_message(
            self.store,
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "todos", "arguments": {}}},
        )
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["content"][0]["type"], "text")

    def test_unknown_tool_reports_invalid_params(self) -> None:
        response = mcp.handle_message(
            self.store,
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "send_message"}},
        )
        self.assertEqual(response["error"]["code"], -32602)

    def test_unknown_method_reports_not_found(self) -> None:
        response = mcp.handle_message(self.store, {"jsonrpc": "2.0", "id": 6, "method": "resources/list"})
        self.assertEqual(response["error"]["code"], -32601)

    def test_non_dict_message_reports_invalid_request(self) -> None:
        response = mcp.handle_message(self.store, ["not", "a", "dict"])
        self.assertEqual(response["error"]["code"], -32600)

    def test_notification_has_no_response(self) -> None:
        response = mcp.handle_message(
            self.store, {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        self.assertIsNone(response)


class ServeStdioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "stdio.sqlite3")

    def _serve(self, lines: list[str]) -> list[dict]:
        stdin = io.StringIO("\n".join(lines) + "\n")
        stdout = io.StringIO()
        code = mcp.serve_stdio(self.store, stdin=stdin, stdout=stdout)
        self.assertEqual(code, 0)
        return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]

    def test_round_trip_and_blank_lines(self) -> None:
        responses = self._serve(
            ["", '{"jsonrpc":"2.0","id":1,"method":"ping"}', "not json", '{"jsonrpc":"2.0","id":2,"method":"tools/list"}']
        )
        self.assertEqual(len(responses), 3)
        self.assertEqual(responses[0]["result"], {})
        self.assertEqual(responses[1]["error"]["code"], -32700)
        self.assertEqual(len(responses[2]["result"]["tools"]), 4)

    def test_notification_emits_nothing(self) -> None:
        responses = self._serve(['{"jsonrpc":"2.0","method":"notifications/initialized"}'])
        self.assertEqual(responses, [])


if __name__ == "__main__":
    unittest.main()
