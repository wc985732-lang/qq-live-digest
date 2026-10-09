"""时间冲突检测（Roadmap A12）回归测试。

守住：只有截止时间相近（默认 30 分钟内）的待办才成组、链式相邻不会滚成一大坨、
已完成默认不参与、window=0 只认同一时刻，以及网页端 /api/conflicts 的鉴权。
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

from qq_live_digest import conflicts  # noqa: E402
from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.store import Store  # noqa: E402
from qq_live_digest.timeutil import iso  # noqa: E402
from qq_live_digest.webapp import TaskWebServer  # noqa: E402

NOW = dt.datetime(2026, 9, 30, 9, 0, 0)


def _task(task_id: int, deadline: dt.datetime | None, *, done: bool = False, summary: str = "") -> dict:
    return {
        "id": task_id,
        "summary": summary or f"任务{task_id}",
        "deadline": iso(deadline) if deadline else "",
        "category": "action",
        "groups": ["学院通知群"],
        "done": done,
    }


class GroupTest(unittest.TestCase):
    def test_spaced_tasks_have_no_conflict(self) -> None:
        tasks = [_task(1, NOW), _task(2, NOW + dt.timedelta(hours=3))]
        self.assertEqual(conflicts.conflict_groups(tasks), [])

    def test_close_tasks_form_one_group(self) -> None:
        tasks = [_task(1, NOW), _task(2, NOW + dt.timedelta(minutes=10))]
        groups = conflicts.conflict_groups(tasks)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 2)
        self.assertEqual([item["id"] for item in groups[0]["items"]], [1, 2])

    def test_window_boundary_is_inclusive(self) -> None:
        tasks = [_task(1, NOW), _task(2, NOW + dt.timedelta(minutes=30))]
        self.assertEqual(len(conflicts.conflict_groups(tasks, window_minutes=30)), 1)
        self.assertEqual(conflicts.conflict_groups(tasks, window_minutes=29), [])

    def test_chain_does_not_over_merge(self) -> None:
        # 每两条只差 20 分钟，但整条链跨度 60 分钟 > 30 分钟窗口，不应滚成一组
        tasks = [
            _task(1, NOW),
            _task(2, NOW + dt.timedelta(minutes=20)),
            _task(3, NOW + dt.timedelta(minutes=40)),
            _task(4, NOW + dt.timedelta(minutes=60)),
        ]
        groups = conflicts.conflict_groups(tasks, window_minutes=30)
        self.assertEqual([group["count"] for group in groups], [2, 2])

    def test_window_zero_only_exact_same_time(self) -> None:
        tasks = [_task(1, NOW), _task(2, NOW), _task(3, NOW + dt.timedelta(minutes=1))]
        groups = conflicts.conflict_groups(tasks, window_minutes=0)
        self.assertEqual(len(groups), 1)
        self.assertEqual([item["id"] for item in groups[0]["items"]], [1, 2])

    def test_done_excluded_unless_asked(self) -> None:
        tasks = [_task(1, NOW), _task(2, NOW, done=True)]
        self.assertEqual(conflicts.conflict_groups(tasks), [])
        self.assertEqual(len(conflicts.conflict_groups(tasks, include_done=True)), 1)

    def test_skips_tasks_without_deadline(self) -> None:
        tasks = [_task(1, NOW), _task(2, None), _task(3, NOW)]
        groups = conflicts.conflict_groups(tasks)
        self.assertEqual([item["id"] for item in groups[0]["items"]], [1, 3])


class PayloadTest(unittest.TestCase):
    def test_payload_shape(self) -> None:
        report = conflicts.payload([_task(1, NOW), _task(2, NOW + dt.timedelta(minutes=5))], now=NOW)
        self.assertTrue(report["ok"])
        self.assertEqual(report["count"], 1)
        self.assertEqual(report["tasks"], 2)
        self.assertEqual(report["window_minutes"], conflicts.DEFAULT_WINDOW_MINUTES)
        self.assertEqual(report["as_of"], iso(NOW))

    def test_render_reports_empty_state(self) -> None:
        text = conflicts.render(conflicts.payload([_task(1, NOW)], now=NOW))
        self.assertIn("没有发现时间相近的待办", text)

    def test_render_lists_groups(self) -> None:
        text = conflicts.render(
            conflicts.payload([_task(1, NOW, summary="交作业"), _task(2, NOW, summary="开班会")], now=NOW)
        )
        self.assertIn("[1]", text)
        self.assertIn("交作业", text)
        self.assertIn("开班会", text)


class HttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "conflicts.sqlite3")
        for key, moment in (("m1", NOW), ("m2", NOW + dt.timedelta(minutes=5))):
            self.store.upsert_task(
                task_key=key, summary=key, category="action", deadline=iso(moment)
            )
        self.settings = Settings(
            group_whitelist=("g1",), web_host="127.0.0.1", web_port=0, web_token="secret"
        )
        self.server = TaskWebServer(self.settings, self.store)
        self.assertTrue(self.server.start())
        self.addCleanup(self.server.stop)
        assert self.server.server is not None
        self.base = f"http://127.0.0.1:{self.server.server.server_address[1]}"

    def test_conflicts_require_token(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(self.base + "/api/conflicts", timeout=5)
        self.assertEqual(context.exception.code, 401)

    def test_conflicts_returns_groups(self) -> None:
        request = urllib.request.Request(self.base + "/api/conflicts", headers={"X-Token": "secret"})
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["tasks"], 2)

if __name__ == "__main__":
    unittest.main()
