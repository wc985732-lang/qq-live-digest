"""只读 REST API（Roadmap A15）回归测试。

守住：接口目录能自描述、token 鉴权在传输层生效、deadlines 只按「有截止时间的待办」
升序返回并标出逾期，且老的 /api/notices 仍可用（网页待办台不回归）。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest import restapi  # noqa: E402
from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.store import Store  # noqa: E402
from qq_live_digest.timeutil import iso  # noqa: E402
from qq_live_digest.webapp import TaskWebServer  # noqa: E402

NOW = dt.datetime(2026, 9, 30, 9, 0, 0)


class PayloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "rest.sqlite3")

    def test_index_lists_endpoints_and_is_read_only(self) -> None:
        payload = restapi.index_payload()
        self.assertTrue(payload["read_only"])
        self.assertTrue(payload["ok"])
        paths = {item["path"] for item in payload["endpoints"]}
        for expected in ("/health", "/api/notifications", "/api/tasks", "/api/deadlines"):
            self.assertIn(expected, paths)
        self.assertFalse(next(i for i in payload["endpoints"] if i["path"] == "/health")["auth"])

    def test_health_payload_shape(self) -> None:
        payload = restapi.health_payload()
        self.assertEqual(payload, {"ok": True, "service": "qq-tasks", "api_version": restapi.API_VERSION, "read_only": True})

    def test_notifications_truncates_body_and_counts(self) -> None:
        self.store.insert_digest(
            kind="window",
            window_start=iso(NOW - dt.timedelta(minutes=10)),
            window_end=iso(NOW),
            body="x" * 900,
            payload=[{"summary": "a"}, {"summary": "b"}],
            message_count=5,
            llm_used=False,
        )
        payload = restapi.notifications(self.store, limit=8)
        self.assertEqual(payload["count"], 1)
        item = payload["items"][0]
        self.assertEqual(item["item_count"], 2)
        self.assertEqual(item["summary"], "2 条重点")
        self.assertEqual(len(item["body"]), 600)

    def _task(self, key: str, summary: str, deadline: str = "", *, status: str = "open") -> int:
        return self.store.upsert_task(
            task_key=key, summary=summary, category="action", deadline=deadline, status=status
        )

    def test_deadlines_are_sorted_and_flag_overdue(self) -> None:
        self._task("late", "已经过了的", iso(NOW - dt.timedelta(hours=2)))
        self._task("soon", "马上到的", iso(NOW + dt.timedelta(hours=3)))
        self._task("note", "没有截止时间")
        payload = restapi.deadlines(self.store, now=NOW)
        self.assertEqual([item["summary"] for item in payload["items"]], ["已经过了的", "马上到的"])
        self.assertEqual(payload["overdue"], 1)
        self.assertTrue(payload["items"][0]["overdue"])
        self.assertFalse(payload["items"][1]["overdue"])
        self.assertAlmostEqual(payload["items"][1]["hours_left"], 3.0, places=1)
        self.assertEqual(payload["as_of"], iso(NOW))

    def test_deadlines_excludes_done_unless_asked(self) -> None:
        task_id = self._task("done", "已完成的", iso(NOW + dt.timedelta(hours=1)))
        self.store.set_task_status(task_id, True)
        self.assertEqual(restapi.deadlines(self.store, now=NOW)["count"], 0)
        with_done = restapi.deadlines(self.store, now=NOW, include_done=True)
        self.assertEqual(with_done["count"], 1)
        self.assertTrue(with_done["items"][0]["done"])

    def test_deadlines_respects_limit(self) -> None:
        for index in range(5):
            self._task(f"t{index}", f"任务{index}", iso(NOW + dt.timedelta(hours=index + 1)))
        payload = restapi.deadlines(self.store, now=NOW, limit=2)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["total"], 5)


class HttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "api.sqlite3")
        self.store.upsert_task(
            task_key="m1",
            summary="提交实验报告",
            category="action",
            deadline=iso(dt.datetime(2026, 9, 30, 18, 0)),
        )
        self.settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            web_host="127.0.0.1",
            web_port=0,
            web_token="secret",
        )
        self.server = TaskWebServer(self.settings, self.store)
        self.assertTrue(self.server.start())
        self.addCleanup(self.server.stop)
        assert self.server.server is not None
        self.base = f"http://127.0.0.1:{self.server.server.server_address[1]}"

    def _get(self, path: str, token: str = "") -> dict:
        headers = {"X-Token": token} if token else {}
        request = urllib.request.Request(self.base + path, headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_health_needs_no_token(self) -> None:
        payload = self._get("/health")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["api_version"], restapi.API_VERSION)

    def test_api_requires_token(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as context:
            self._get("/api")
        self.assertEqual(context.exception.code, 401)

    def test_api_index_lists_endpoints(self) -> None:
        payload = self._get("/api", token="secret")
        paths = {item["path"] for item in payload["endpoints"]}
        self.assertIn("/api/deadlines", paths)

    def test_notifications_alias_matches_legacy_notices(self) -> None:
        current = self._get("/api/notifications", token="secret")
        legacy = self._get("/api/notices", token="secret")
        self.assertEqual(current, legacy)

    def test_deadlines_endpoint_returns_sorted_items(self) -> None:
        payload = self._get("/api/deadlines", token="secret")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["summary"], "提交实验报告")


if __name__ == "__main__":
    unittest.main()
