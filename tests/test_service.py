from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.push import Pusher  # noqa: E402
from qq_live_digest.retry import LLMRequestError  # noqa: E402
from qq_live_digest.service import DigestService  # noqa: E402
from qq_live_digest.store import Store  # noqa: E402
from qq_live_digest.summarizer import build_digest  # noqa: E402
from qq_live_digest.timeutil import iso  # noqa: E402

NOW = dt.datetime(2026, 9, 28, 18, 0, 0)


class FakeBot:
    def __init__(self) -> None:
        self.api = None
        self.loop = None
        self.is_alive = False
        self.is_ready = False
        self.last_error = ""
        self.starts = 0
        self.stops = 0

    def start(self) -> bool:
        self.starts += 1
        self.is_alive = True
        return True

    def stop(self) -> None:
        self.stops += 1
        self.is_alive = False

    def wait_ready(self, timeout: float = 0) -> bool:
        return True


class RecordingPusher(Pusher):
    name = "webhook"
    tier = 0

    def __init__(self, *, fail_times: int = 0) -> None:
        super().__init__("test-target")
        self.fail_times = fail_times
        self.calls: list[str] = []

    def send(self, title: str, body: str, *, summary: str = "", html: str = "") -> None:
        self.calls.append(body)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("模拟推送失败")


def record(
    msg_id: str,
    content: str,
    *,
    minutes_ago: int,
    group: str = "g1",
    sender: str = "辅导员",
) -> dict:
    stamp = NOW - dt.timedelta(minutes=minutes_ago)
    return {
        "msg_id": msg_id,
        "source": "qqbot",
        "event": "GROUP_MESSAGE_CREATE",
        "group_id": group,
        "group_name": "学院通知群",
        "sender_id": "u1",
        "sender_name": sender,
        "ts": iso(stamp),
        "received_at": iso(stamp),
        "content": content,
    }


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            window_minutes=10,
            min_score=3,
            delivery_retry_seconds=0,
        )
        self.store = Store(Path(self.tmp.name) / "service.sqlite3")
        self.pusher = RecordingPusher()
        self.bot = FakeBot()
        self.service = DigestService(
            self.settings,
            store=self.store,
            bot=self.bot,
            pushers=[self.pusher],
        )

    def test_task_url_uses_configured_public_base(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            web_base_url="https://qq-digest.tailnet.ts.net",
            web_token="secret",
        )
        store = Store(Path(self.tmp.name) / "public-url.sqlite3")
        service = DigestService(settings, store=store, bot=self.bot, pushers=[self.pusher])
        self.assertEqual(
            service._task_url(7),
            "https://qq-digest.tailnet.ts.net/?token=secret#task-7",
        )

    def test_window_flush_pushes_focus_items_once(self) -> None:
        self.assertTrue(self.service.on_message(record("m1", "【学院通知】关于2026年国庆节放假安排的通知", minutes_ago=11)))
        produced = self.service.tick(now=NOW)
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].kind, "window")
        self.assertEqual(len(self.pusher.calls), 1)
        self.assertIn("国庆节", self.pusher.calls[0])

        # 再次调度不会重复推送
        self.assertEqual(self.service.tick(now=NOW), [])
        self.assertEqual(len(self.pusher.calls), 1)
        self.assertEqual(self.store.counts()["unprocessed"], 0)

    def test_digest_writes_open_task(self) -> None:
        self.service.on_message(record("m1", "请各班班长今天18:00前提交材料", minutes_ago=11))
        produced = self.service.tick(now=NOW)
        self.assertEqual(len(produced), 1)
        tasks = self.store.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "open")
        self.assertIn("提交材料", tasks[0]["summary"])
        self.assertEqual(tasks[0]["groups"], ["学院通知群"])

    def test_within_window_waits_then_flushes(self) -> None:
        self.service.on_message(record("m1", "【学院通知】放假安排", minutes_ago=1))
        self.assertEqual(self.service.tick(now=NOW), [])
        self.assertEqual(self.store.counts()["unprocessed"], 1)

        later = NOW + dt.timedelta(minutes=10)
        produced = self.service.tick(now=later)
        self.assertEqual(len(produced), 1)
        self.assertEqual(len(self.pusher.calls), 1)

    def test_immediate_group_flushes_without_waiting_window(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "测试群"},
            immediate_groups=("g1",),
            window_minutes=30,
            min_score=3,
            delivery_retry_seconds=0,
        )
        store = Store(Path(self.tmp.name) / "immediate.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        service.on_message(record("m1", "【学院通知】关于2026年国庆节放假安排的通知", minutes_ago=1))
        produced = service.tick(now=NOW)
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].kind, "window")
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(store.counts()["unprocessed"], 0)

    def test_chatter_window_is_silent(self) -> None:
        self.service.on_message(record("m1", "哈哈哈哈", minutes_ago=11, sender="同学A"))
        produced = self.service.tick(now=NOW)
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].kind, "silent")
        self.assertEqual(self.pusher.calls, [])
        self.assertEqual(self.store.counts()["unprocessed"], 0)
        self.assertEqual(self.store.counts()["digests"], 1)

    def test_urgent_pushed_immediately(self) -> None:
        self.service.on_message(record("m1", "请各班班长今天18:00前提交材料", minutes_ago=1))
        produced = self.service.tick(now=NOW)
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].kind, "urgent")
        self.assertEqual(len(self.pusher.calls), 1)
        self.assertIn("截止", self.pusher.calls[0])
        self.assertEqual(produced[0].items[0]["category"], "urgent")

    def test_quiet_group_chatter_is_silent_and_never_becomes_a_task(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "电气工程学院2026级新生交流群"},
            quiet_groups=("g1",),
            window_minutes=10,
            min_score=3,
            delivery_retry_seconds=0,
        )
        store = Store(Path(self.tmp.name) / "quiet-chatter.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        service.on_message(record(
            "m1",
            "不会的吧，我今天问4栋那个快递站明天可以寄快递吗他还说可以的",
            minutes_ago=1,
            sender="26测仪",
        ))
        self.assertEqual(service.tick(now=NOW), [])
        produced = service.tick(now=NOW + dt.timedelta(minutes=11))
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].kind, "silent")
        self.assertEqual(pusher.calls, [])
        self.assertEqual(store.list_tasks(), [])

    def test_quiet_group_real_notice_goes_to_window_and_task(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "电气工程学院2026级新生交流群"},
            quiet_groups=("g1",),
            window_minutes=10,
            min_score=3,
            delivery_retry_seconds=0,
        )
        store = Store(Path(self.tmp.name) / "quiet-notice.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        service.on_message(record(
            "m2",
            "请各班班长明天18:00前提交材料",
            minutes_ago=1,
            sender="辅导员",
        ))
        self.assertEqual(service.tick(now=NOW), [])
        produced = service.tick(now=NOW + dt.timedelta(minutes=11))
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].kind, "window")
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(len(store.list_tasks()), 1)

    def test_catchup_total_failure_retries_after_five_minutes(self) -> None:
        self.service.settings.catchup_enabled = True
        def fake_backfill(
            settings, store, logger=None, *, now=None, force=False, stats=None, attachment_sink=None
        ):
            if stats is not None:
                stats.update({"groups": 1, "ok_groups": 0, "failed_groups": 1, "inserted": 0})
            return 0

        with mock.patch("qq_live_digest.service.backfill", side_effect=fake_backfill):
            self.assertEqual(self.service.run_catchup(now=NOW), 0)
            self.assertIsNone(self.service.last_catchup_at)
            self.assertEqual(self.service.last_catchup_attempt_at, NOW)

            self.service._maybe_catchup(NOW + dt.timedelta(minutes=1))
            self.assertIsNone(self.service._catchup_thread)

            self.service._maybe_catchup(NOW + dt.timedelta(minutes=6))
            self.assertIsNotNone(self.service._catchup_thread)
            self.service._catchup_thread.join(timeout=5)

    def test_duplicate_and_whitelist_filter(self) -> None:
        payload = record("m1", "【学院通知】放假安排", minutes_ago=11)
        self.assertTrue(self.service.on_message(payload))
        self.assertFalse(self.service.on_message(payload))
        self.assertFalse(self.service.on_message(record("m2", "通知", minutes_ago=11, group="g2")))
        self.assertEqual(self.store.counts()["messages"], 1)

    def test_failed_push_is_retried_in_same_tick(self) -> None:
        self.pusher.fail_times = 1
        self.service.on_message(record("m1", "【学院通知】放假安排", minutes_ago=11))
        produced = self.service.tick(now=NOW)
        self.assertEqual(len(produced), 1)
        # retry_seconds=0：同一轮调度内失败重试一次并成功
        self.assertEqual(len(self.pusher.calls), 2)
        counts = self.store.counts()
        self.assertEqual(counts["deliveries_sent"], 1)
        self.assertEqual(counts["deliveries_failed"], 0)

    def test_failed_push_waits_for_cooldown(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            window_minutes=10,
            delivery_retry_seconds=3600,
        )
        store = Store(Path(self.tmp.name) / "cooldown.sqlite3")
        pusher = RecordingPusher(fail_times=1)
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        service.on_message(record("m1", "【学院通知】放假安排", minutes_ago=11))
        service.tick(now=NOW)
        self.assertEqual(len(pusher.calls), 1)
        service.tick(now=NOW + dt.timedelta(seconds=30))
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(store.counts()["deliveries_failed"], 1)
        # 冷却结束后手动重试成功
        self.assertEqual(len(store.pending_deliveries(max_attempts=4, retry_seconds=0)), 1)
        outcomes = service.push_manager().retry_pending(retry_seconds=0)
        self.assertTrue(any(item.ok for item in outcomes))
        self.assertEqual(len(pusher.calls), 2)

    def test_status_reports_components(self) -> None:
        status = self.service.status()
        self.assertIn("counts", status)
        self.assertEqual(status["channels"], ["webhook:test-target"])

    def test_start_and_stop_with_injected_bot(self) -> None:
        self.service.start(start_bot=False)
        self.service.stop()
        status = self.service.status()
        self.assertEqual(status["started_at"], "")

    def test_deadline_reminders_fire_once_and_cover_tomorrow(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            window_minutes=10,
            min_score=3,
            catchup_enabled=False,
            deadline_reminders_enabled=True,
            deadline_morning="07:30",
            deadline_evening="21:00",
        )
        store = Store(Path(self.tmp.name) / "reminders.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        start = dt.datetime(2026, 9, 29, 6, 0)

        store.upsert_task(
            task_key="m1",
            summary="今天提交材料",
            category="action",
            deadline=iso(dt.datetime(2026, 9, 29, 18, 0)),
            groups=["学院通知群"],
        )
        store.upsert_task(
            task_key="m2",
            summary="明天提交报名表",
            category="action",
            deadline=iso(dt.datetime(2026, 9, 30, 12, 0)),
            groups=["学院通知群"],
        )

        service.tick(now=dt.datetime(2026, 9, 29, 7, 31))
        self.assertEqual(len(pusher.calls), 1)
        self.assertIn("今日截止", pusher.calls[0])
        self.assertIn("2026-09-29 18:00", pusher.calls[0])
        self.assertNotIn("2026-09-30", pusher.calls[0])

        service.tick(now=dt.datetime(2026, 9, 29, 8, 0))
        self.assertEqual(len(pusher.calls), 1)

        service.tick(now=dt.datetime(2026, 9, 29, 21, 0))
        self.assertEqual(len(pusher.calls), 2)
        self.assertIn("截止提醒", pusher.calls[1])
        self.assertIn("2026-09-30 12:00", pusher.calls[1])
        self.assertNotIn("2026-09-29 18:00", pusher.calls[1])

    def test_late_evening_start_skips_morning_reminder(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            window_minutes=10,
            min_score=3,
            catchup_enabled=False,
            deadline_reminders_enabled=True,
            deadline_morning="07:30",
            deadline_evening="21:00",
        )
        store = Store(Path(self.tmp.name) / "reminders-late.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        store.upsert_task(
            task_key="m1",
            summary="明天提交报名表",
            category="action",
            deadline=iso(dt.datetime(2026, 9, 30, 12, 0)),
            groups=["学院通知群"],
        )

        service.tick(now=dt.datetime(2026, 9, 29, 22, 0))
        self.assertEqual(len(pusher.calls), 1)
        self.assertIn("截止提醒", pusher.calls[0])
        self.assertNotIn("今日截止", pusher.calls[0])
        self.assertIn("2026-09-30 12:00", pusher.calls[0])


    def test_ambiguous_action_becomes_candidate(self) -> None:
        self.service.on_message(record("m1", "记得明天可能要交报名表", minutes_ago=11, sender="同学"))
        self.service.tick(now=NOW)
        tasks = self.store.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "candidate")
        self.assertLess(tasks[0]["confidence"], 0.75)

    def test_candidate_confirmation_retries_and_is_daily_limited(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            delivery_retry_seconds=0,
            candidate_push_max_per_tick=1,
        )
        store = Store(Path(self.tmp.name) / "candidate-push.sqlite3")
        pusher = RecordingPusher(fail_times=1)
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        task_id = store.upsert_task(
            task_key="candidate-1",
            summary="可能要交报名表",
            category="action",
            evidence="记得看一下报名表",
            status="candidate",
            confidence=0.7,
        )

        service.tick(now=NOW)
        self.assertEqual(store.get_task(task_id)["status"], "candidate")
        self.assertEqual(store.get_task(task_id)["last_reminded_at"], "")

        service.tick(now=NOW + dt.timedelta(minutes=1))
        self.assertEqual(len(pusher.calls), 2)
        self.assertTrue(store.get_task(task_id)["last_reminded_at"])
        self.assertEqual(store.get_task(task_id)["status"], "candidate")

        service.tick(now=NOW + dt.timedelta(hours=2))
        self.assertEqual(len(pusher.calls), 2)

    def test_overdue_open_task_is_reminded_but_dismissed_is_not(self) -> None:
        clear = Settings(
            group_whitelist=("g1",),
            catchup_enabled=False,
            deadline_morning="07:30",
            deadline_evening="21:00",
        )
        store = Store(Path(self.tmp.name) / "overdue.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(clear, store=store, bot=FakeBot(), pushers=[pusher])
        open_id = store.upsert_task(
            task_key="open-overdue",
            summary="昨天应交的材料",
            category="action",
            deadline=iso(dt.datetime(2026, 9, 28, 18, 0)),
        )
        dismissed_id = store.upsert_task(
            task_key="dismissed",
            summary="已忽略事项",
            category="action",
            deadline=iso(dt.datetime(2026, 9, 28, 19, 0)),
        )
        store.apply_task_action(dismissed_id, "dismiss")

        service.tick(now=NOW.replace(hour=8, minute=0))
        self.assertEqual(len(pusher.calls), 1)
        self.assertIn("昨天应交的材料", pusher.calls[0])
        self.assertNotIn("已忽略事项", pusher.calls[0])
        self.assertTrue(store.get_task(open_id)["last_reminded_at"])
        self.assertEqual(store.get_task(dismissed_id)["remind_count"], 0)

        # 同一任务在同一天再次调度不会重复提醒。
        service.tick(now=NOW.replace(hour=9, minute=0))
        self.assertEqual(len(pusher.calls), 1)


    def test_deadline_reminder_without_channel_keeps_retry_key(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            catchup_enabled=False,
            deadline_morning="07:30",
            deadline_evening="21:00",
        )
        store = Store(Path(self.tmp.name) / "no-channel.sqlite3")
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[])
        store.upsert_task(
            task_key="m1",
            summary="今天提交材料",
            category="action",
            deadline=iso(dt.datetime(2026, 9, 28, 18, 0)),
            groups=["学院通知群"],
        )
        service.tick(now=NOW.replace(hour=7, minute=31))
        self.assertEqual(store.meta_get("deadline_reminder:morning:2026-09-28"), "")

        pusher = RecordingPusher()
        service._pushers = [pusher]
        service._push_manager = None
        service.tick(now=NOW.replace(hour=7, minute=32))
        self.assertEqual(len(pusher.calls), 1)
        self.assertTrue(store.meta_get("deadline_reminder:morning:2026-09-28"))

    def test_retryable_llm_failure_defers_batch_then_recovers(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            window_minutes=1,
            min_score=3,
            delivery_retry_seconds=0,
            dashscope_api_key="test-key",
            llm_max_retries=0,
            llm_retry_backoff=0.0,
            llm_defer_max_attempts=3,
        )
        store = Store(Path(self.tmp.name) / "llm-defer.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        service.on_message(record("m1", "【学院通知】关于2026年国庆节放假安排的通知", minutes_ago=2))

        with mock.patch(
            "qq_digest.refine_items",
            side_effect=LLMRequestError("LLM 请求失败: connection refused"),
        ):
            service.tick(now=NOW)
        self.assertEqual(len(pusher.calls), 0)
        self.assertEqual(store.counts()["unprocessed"], 1)
        self.assertEqual(store.meta_get("llm_defer:count"), "1")

        with mock.patch(
            "qq_digest.refine_items",
            side_effect=lambda items, *args, **kwargs: items,
        ):
            service.tick(now=NOW + dt.timedelta(minutes=1))
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(store.counts()["unprocessed"], 0)
        self.assertEqual(store.meta_get("llm_defer:count"), "0")

    def test_non_retryable_llm_failure_falls_back_without_blocking(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            window_minutes=1,
            min_score=3,
            delivery_retry_seconds=0,
            dashscope_api_key="test-key",
            llm_max_retries=0,
            llm_retry_backoff=0.0,
        )
        store = Store(Path(self.tmp.name) / "llm-auth.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        service.on_message(record("m1", "【学院通知】关于2026年国庆节放假安排的通知", minutes_ago=2))

        with mock.patch(
            "qq_digest.refine_items",
            side_effect=LLMRequestError("LLM HTTP 401: bad key", status=401),
        ):
            service.tick(now=NOW)
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(store.counts()["unprocessed"], 0)
        self.assertEqual(store.meta_get("llm_fallbacks_total"), "1")

    def test_quiet_hours_hold_batch_until_morning(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            window_minutes=1,
            min_score=3,
            delivery_retry_seconds=0,
            quiet_hours="23:00-07:00",
        )
        store = Store(Path(self.tmp.name) / "quiet.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        service.on_message(record("m1", "【学院通知】关于2026年国庆节放假安排的通知", minutes_ago=2))

        service.tick(now=NOW.replace(hour=23, minute=30))
        self.assertEqual(len(pusher.calls), 0)
        self.assertEqual(store.counts()["unprocessed"], 1)
        self.assertEqual(service.last_defer_reason, "quiet_hours")

        morning = (NOW + dt.timedelta(days=1)).replace(hour=7, minute=1)
        service.tick(now=morning)
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(store.counts()["unprocessed"], 0)

    def test_daily_push_budget_holds_extra_batches(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            window_minutes=1,
            min_score=3,
            delivery_retry_seconds=0,
            push_daily_budget=1,
        )
        store = Store(Path(self.tmp.name) / "budget.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        service.on_message(record("m1", "【学院通知】关于2026年国庆节放假安排的通知", minutes_ago=11))
        service.tick(now=NOW)
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(store.meta_get("push_budget:2026-09-28"), "1")

        service.on_message(record("m2", "【学院通知】关于2026年国庆节后课程调整的通知", minutes_ago=11))
        service.tick(now=NOW + dt.timedelta(minutes=1))
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(store.counts()["unprocessed"], 1)
        self.assertEqual(service.last_defer_reason, "daily_budget")

    def test_urgent_bypasses_quiet_hours(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            window_minutes=30,
            min_score=3,
            delivery_retry_seconds=0,
            quiet_hours="23:00-07:00",
        )
        store = Store(Path(self.tmp.name) / "urgent-night.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        service.on_message(record("m1", "请各班班长今天18:00前提交材料", minutes_ago=1))

        service.tick(now=NOW.replace(hour=23, minute=30))
        self.assertEqual(len(pusher.calls), 1)

if __name__ == "__main__":
    unittest.main()
