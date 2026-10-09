"""MCP 接口（Roadmap A1 只读 + A2 可写待办）回归测试。

守住：5 个只读 Tool + 2 个可写 Tool（仅 tasks、必须 confirm、逐次审计）；
没有任何 QQ 发送 / 删除 / 改设置入口；call_tool 逐个可用；
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
    def test_lists_five_read_and_two_write_tools(self) -> None:
        tools = mcp.list_tools()
        names = {tool["name"] for tool in tools}
        self.assertEqual(
            names,
            {
                "recent_notifications",
                "todos",
                "deadlines",
                "search_messages",
                "recent_audit",
                "set_task_done",
                "add_task",
            },
        )
        self.assertEqual(len(tools), 7)
        for tool in tools:
            self.assertIn("description", tool)
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_read_tools_marked_read_only_and_write_tools_not(self) -> None:
        by_name = {tool["name"]: tool for tool in mcp.list_tools()}
        for name in (
            "recent_notifications",
            "todos",
            "deadlines",
            "search_messages",
            "recent_audit",
        ):
            self.assertTrue(by_name[name]["annotations"]["readOnlyHint"], name)
        for name in ("set_task_done", "add_task"):
            self.assertFalse(by_name[name]["annotations"]["readOnlyHint"], name)
            self.assertIn("confirm", by_name[name]["inputSchema"]["required"], name)

    def test_no_qq_send_or_delete_tools(self) -> None:
        names = {tool["name"] for tool in mcp.list_tools()}
        for forbidden in (
            "send",
            "send_message",
            "send_qq",
            "delete",
            "delete_task",
            "remove_group",
            "write",
            "set_settings",
        ):
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

    def _seed_task(self, summary: str = "交报告") -> int:
        return self.store.upsert_task(
            task_key="seed",
            summary=summary,
            category="action",
            deadline=iso(NOW + dt.timedelta(hours=5)),
        )

    def test_set_task_done_without_confirm_is_refused_and_audited(self) -> None:
        task_id = self._seed_task()
        result = mcp.call_tool(self.store, "set_task_done", {"task_id": task_id})
        self.assertTrue(result["isError"])
        self.assertFalse(json.loads(result["content"][0]["text"])["ok"])
        self.assertEqual(self.store.get_task(task_id)["status"], "open")
        audit = self.store.recent_audit(limit=5)
        self.assertEqual(len(audit), 1)
        self.assertFalse(audit[0]["ok"])
        self.assertEqual(audit[0]["tool"], "set_task_done")

    def test_set_task_done_with_confirm_closes_and_audits(self) -> None:
        task_id = self._seed_task()
        result = mcp.call_tool(
            self.store, "set_task_done", {"task_id": task_id, "confirm": True}
        )
        self.assertFalse(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["changed"])
        self.assertEqual(self.store.get_task(task_id)["status"], "done")
        audit = self.store.recent_audit(limit=5)
        self.assertTrue(audit[0]["ok"])
        self.assertEqual(audit[0]["target"], f"task:{task_id}")

    def test_set_task_done_unknown_id_is_audited_error(self) -> None:
        result = mcp.call_tool(
            self.store, "set_task_done", {"task_id": 9999, "confirm": True}
        )
        self.assertTrue(result["isError"])
        audit = self.store.recent_audit(limit=5)
        self.assertFalse(audit[0]["ok"])
        self.assertEqual(audit[0]["target"], "task:9999")

    def test_add_task_without_confirm_is_refused(self) -> None:
        result = mcp.call_tool(self.store, "add_task", {"summary": "开组会"})
        self.assertTrue(result["isError"])
        self.assertEqual(self.store.list_tasks(), [])
        self.assertFalse(self.store.recent_audit(limit=1)[0]["ok"])

    def test_add_task_with_confirm_creates_open_task_and_audits(self) -> None:
        result = mcp.call_tool(
            self.store,
            "add_task",
            {
                "summary": "交实验报告",
                "action": "发邮件",
                "deadline": "2026-10-20",
                "confirm": True,
            },
        )
        self.assertFalse(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertTrue(payload["ok"])
        task = self.store.get_task(payload["task_id"])
        self.assertEqual(task["status"], "open")
        self.assertEqual(task["summary"], "交实验报告")
        self.assertEqual(task["source"], "mcp")
        audit = self.store.recent_audit(limit=1)[0]
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["tool"], "add_task")

    def test_add_task_rejects_bad_deadline(self) -> None:
        result = mcp.call_tool(
            self.store,
            "add_task",
            {"summary": "x", "deadline": "10/20/2026", "confirm": True},
        )
        self.assertTrue(result["isError"])
        self.assertEqual(self.store.list_tasks(), [])

    def test_add_task_rejects_blank_summary(self) -> None:
        result = mcp.call_tool(
            self.store, "add_task", {"summary": "   ", "confirm": True}
        )
        self.assertTrue(result["isError"])
        self.assertEqual(self.store.list_tasks(), [])

    def test_recent_audit_lists_newest_first(self) -> None:
        self.store.record_audit(tool="a", target="t1", ok=True)
        self.store.record_audit(tool="b", target="t2", ok=False)
        payload = json.loads(
            mcp.call_tool(self.store, "recent_audit", {"limit": 5})["content"][0]["text"]
        )
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["items"][0]["tool"], "b")
        self.assertFalse(payload["items"][0]["ok"])


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
        self.assertEqual(len(names), 7)

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
        self.assertEqual(len(responses[2]["result"]["tools"]), 7)

    def test_notification_emits_nothing(self) -> None:
        responses = self._serve(['{"jsonrpc":"2.0","method":"notifications/initialized"}'])
        self.assertEqual(responses, [])


if __name__ == "__main__":
    unittest.main()
