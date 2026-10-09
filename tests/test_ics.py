"""ICS / 日历导出（Roadmap A13）回归测试。

守住：只有带截止时间的待办才导出、全天 vs 定时的区分、RFC 5545 转义与折行、
CRLF 行尾，以及网页端 /api/calendar.ics 的鉴权与内容类型。
"""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest import ics  # noqa: E402
from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.store import Store  # noqa: E402
from qq_live_digest.timeutil import iso  # noqa: E402
from qq_live_digest.webapp import TaskWebServer  # noqa: E402

# 固定 +08:00 偏移：DTSTAMP 要按 UTC 写出，若用裸 datetime 会吃运行机器的本地时区，
# 在 UTC 的 CI runner 上会把 09:00 当成 09:00Z。显式带时区让断言与机器无关。
CST = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 9, 30, 9, 0, 0, tzinfo=CST)


def _task(**overrides):
    task = {
        "id": 1,
        "summary": "提交实验报告",
        "action": "上传 PDF 到教务系统",
        "category": "action",
        "groups": ["学院通知群"],
        "deadline": iso(dt.datetime(2026, 9, 30, 18, 0)),
        "done": False,
    }
    task.update(overrides)
    return task


class TextTest(unittest.TestCase):
    def test_escape_text(self) -> None:
        self.assertEqual(ics.escape_text("a\\b;c,d\ne"), "a\\\\b\\;c\\,d\\ne")

    def test_fold_line_keeps_short_lines(self) -> None:
        self.assertEqual(ics.fold_line("SUMMARY:短"), "SUMMARY:短")

    def test_fold_line_wraps_on_octet_boundaries(self) -> None:
        line = "SUMMARY:" + "中" * 60
        folded = ics.fold_line(line)
        self.assertIn("\r\n ", folded)
        for index, segment in enumerate(folded.split("\r\n")):
            body = segment[1:] if index else segment
            self.assertLessEqual(len(body.encode("utf-8")), ics.MAX_LINE_OCTETS)
        self.assertEqual(folded.replace("\r\n ", ""), line)


class EventTest(unittest.TestCase):
    def test_skips_tasks_without_deadline(self) -> None:
        events = ics.build_events([_task(deadline=""), _task(id=2)])
        self.assertEqual([event["id"] for event in events], [2])

    def test_all_day_detected_at_midnight(self) -> None:
        events = ics.build_events([_task(deadline=iso(dt.datetime(2026, 9, 30, 0, 0)))])
        self.assertTrue(events[0]["all_day"])
        self.assertFalse(ics.build_events([_task()])[0]["all_day"])

    def test_excludes_done_unless_asked(self) -> None:
        done = _task(done=True)
        self.assertEqual(ics.build_events([done]), [])
        self.assertEqual(len(ics.build_events([done], include_done=True)), 1)

    def test_events_are_sorted_by_start(self) -> None:
        late = _task(id=2, deadline=iso(dt.datetime(2026, 10, 2, 9, 0)))
        soon = _task(id=1, deadline=iso(dt.datetime(2026, 9, 30, 9, 0)))
        events = ics.build_events([late, soon])
        self.assertEqual([event["id"] for event in events], [1, 2])


class CalendarTest(unittest.TestCase):
    def test_calendar_structure_and_crlf(self) -> None:
        text = ics.build_calendar([_task()], now=NOW)
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))
        self.assertNotIn("\n\n", text)
        for line in text.split("\r\n"):
            self.assertNotIn("\n", line)
        self.assertIn("BEGIN:VEVENT\r\n", text)
        self.assertIn("UID:qq-digest-task-1@qq-live-digest\r\n", text)
        self.assertIn("SUMMARY:提交实验报告\r\n", text)
        self.assertIn("DTSTART:20260930T180000\r\n", text)
        self.assertIn("DTSTAMP:20260930T010000Z\r\n", text)

    def test_all_day_uses_date_values(self) -> None:
        text = ics.build_calendar([_task(deadline=iso(dt.datetime(2026, 9, 30, 0, 0)))], now=NOW)
        self.assertIn("DTSTART;VALUE=DATE:20260930\r\n", text)
        self.assertIn("DTEND;VALUE=DATE:20261001\r\n", text)

    def test_empty_calendar_is_still_valid(self) -> None:
        text = ics.build_calendar([], now=NOW)
        self.assertIn("BEGIN:VCALENDAR\r\n", text)
        self.assertNotIn("BEGIN:VEVENT", text)
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))

    def test_payload_counts(self) -> None:
        report = ics.payload(
            [_task(), _task(id=2, deadline=iso(dt.datetime(2026, 9, 30, 0, 0))), _task(id=3, deadline="")],
            now=NOW,
        )
        self.assertEqual(report["events"], 2)
        self.assertEqual(report["all_day"], 1)
        self.assertEqual(report["first"], iso(dt.datetime(2026, 9, 30, 0, 0)))


class HttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "ics.sqlite3")
        self.store.upsert_task(
            task_key="m1",
            summary="提交实验报告",
            category="action",
            deadline=iso(dt.datetime(2026, 9, 30, 18, 0)),
        )
        self.settings = Settings(
            group_whitelist=("g1",),
            web_host="127.0.0.1",
            web_port=0,
            web_token="secret",
        )
        self.server = TaskWebServer(self.settings, self.store)
        self.assertTrue(self.server.start())
        self.addCleanup(self.server.stop)
        assert self.server.server is not None
        self.base = f"http://127.0.0.1:{self.server.server.server_address[1]}"

    def test_calendar_requires_token(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(self.base + "/api/calendar.ics", timeout=5)
        self.assertEqual(context.exception.code, 401)

    def test_calendar_returns_ics(self) -> None:
        request = urllib.request.Request(self.base + "/api/calendar.ics", headers={"X-Token": "secret"})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertTrue(response.headers["Content-Type"].startswith("text/calendar"))
            body = response.read().decode("utf-8")
        self.assertIn("BEGIN:VCALENDAR", body)
        self.assertIn("SUMMARY:提交实验报告", body)


if __name__ == "__main__":
    unittest.main()
