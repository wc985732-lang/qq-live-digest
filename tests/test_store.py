from __future__ import annotations

import sys
import sqlite3
import tempfile
import datetime as dt
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest.store import Store  # noqa: E402
from qq_live_digest.timeutil import iso, now_local  # noqa: E402


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "test.sqlite3")

    def _insert(
        self, msg_id: str, content: str = "通知", group: str = "g1", source_text: str = ""
    ) -> bool:
        return self.store.insert_message(
            msg_id=msg_id,
            group_id=group,
            content=content,
            source_text=source_text,
            sender_name="辅导员",
            group_name="学院群",
        )

    def test_insert_dedupes_by_msg_id(self) -> None:
        self.assertTrue(self._insert("m1"))
        self.assertFalse(self._insert("m1", content="重复"))
        self.assertEqual(self.store.counts()["messages"], 1)
        self.assertEqual(self.store.counts()["unprocessed"], 1)

    def test_unprocessed_and_mark_processed(self) -> None:
        self._insert("m1")
        self._insert("m2")
        rows = self.store.unprocessed_messages()
        self.assertEqual([row["msg_id"] for row in rows], ["m1", "m2"])
        digest_id = self.store.insert_digest(
            kind="window",
            window_start=iso(now_local()),
            window_end=iso(now_local()),
            body="正文",
            payload=[{"summary": "x"}],
            message_count=2,
            llm_used=False,
        )
        self.assertGreater(digest_id, 0)
        self.store.mark_processed(["m1", "m2"], digest_id)
        self.assertEqual(self.store.unprocessed_messages(), [])
        self.assertEqual(self.store.counts()["unprocessed"], 0)
        digest = self.store.get_digest(digest_id)
        self.assertIsNotNone(digest)
        assert digest is not None
        self.assertEqual(digest["items"], [{"summary": "x"}])

    def test_delivery_claim_dedupe_and_retry(self) -> None:
        digest_id = self.store.insert_digest(
            kind="window",
            window_start="",
            window_end="",
            body="正文",
            payload=[],
            message_count=1,
            llm_used=False,
        )
        send, delivery_id = self.store.claim_delivery(
            digest_id=digest_id,
            channel="qq-bot",
            target="openid-1",
            dedupe_key=f"digest:{digest_id}",
            max_attempts=3,
            retry_seconds=0,
        )
        self.assertTrue(send)
        # 同一投递在冷却中不会重复发送
        send_again, same_id = self.store.claim_delivery(
            digest_id=digest_id,
            channel="qq-bot",
            target="openid-1",
            dedupe_key=f"digest:{digest_id}",
            max_attempts=3,
            retry_seconds=3600,
        )
        self.assertFalse(send_again)
        self.assertEqual(same_id, delivery_id)

        self.store.mark_delivery(delivery_id or 0, ok=True)
        send_after_sent, _ = self.store.claim_delivery(
            digest_id=digest_id,
            channel="qq-bot",
            target="openid-1",
            dedupe_key=f"digest:{digest_id}",
            max_attempts=3,
            retry_seconds=0,
        )
        self.assertFalse(send_after_sent)

    def test_pending_deliveries_and_attempt_limit(self) -> None:
        digest_id = self.store.insert_digest(
            kind="urgent",
            window_start="",
            window_end="2026-09-28 18:00",
            body="紧急正文",
            payload=[],
            message_count=1,
            llm_used=False,
        )
        _, delivery_id = self.store.claim_delivery(
            digest_id=digest_id,
            channel="webhook",
            target="example.com#abcd",
            dedupe_key=f"digest:{digest_id}",
            max_attempts=2,
            retry_seconds=0,
        )
        self.store.mark_delivery(delivery_id or 0, ok=False, error="boom")
        pending = self.store.pending_deliveries(max_attempts=2, retry_seconds=0)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["body"], "紧急正文")

        # attempts 达到上限后不再重试
        self.store.increment_delivery_attempt(delivery_id or 0)
        self.store.mark_delivery(delivery_id or 0, ok=False, error="boom2")
        self.assertEqual(self.store.pending_deliveries(max_attempts=2, retry_seconds=0), [])

    def test_meta_and_prune(self) -> None:
        self.store.insert_message(
            msg_id="old",
            group_id="g1",
            content="旧消息",
            received_at=iso(now_local() - dt.timedelta(days=40)),
        )
        self.store.mark_processed(["old"], None)
        self.store.meta_set("last_prune", "2026-09-28 00:00:00")
        self.assertEqual(self.store.meta_get("last_prune"), "2026-09-28 00:00:00")
        removed = self.store.prune(retention_days=30)
        self.assertEqual(removed, 1)
        self.assertEqual(self.store.counts()["messages"], 0)

    def test_jsonl_audit_written(self) -> None:
        self._insert("m1")
        path = Path(self.tmp.name) / "messages.jsonl"
        self.assertTrue(path.is_file())
        self.assertIn("m1", path.read_text(encoding="utf-8"))


    def test_old_task_database_migrates_without_data_loss(self) -> None:
        path = Path(self.tmp.name) / "old.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_key TEXT NOT NULL UNIQUE,
                summary TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'info',
                importance INTEGER NOT NULL DEFAULT 3,
                deadline TEXT NOT NULL DEFAULT '',
                groups TEXT NOT NULL DEFAULT '[]',
                sender TEXT NOT NULL DEFAULT '',
                evidence TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                digest_id INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                done_at TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO tasks
                (task_key, summary, status, created_at, updated_at)
            VALUES ('old', '旧任务', 'open', '2026-09-01 08:00:00', '2026-09-01 08:00:00');
            """
        )
        connection.commit()
        connection.close()

        migrated = Store(path)
        tasks = migrated.list_tasks()
        self.assertEqual(tasks[0]["summary"], "旧任务")
        self.assertEqual(tasks[0]["confidence"], 0.0)
        with migrated._connect() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()}
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("confirmed_at", columns)
        self.assertIn("last_reminded_at", columns)
        self.assertIn("task_events", tables)

    def test_task_keeps_structured_details_and_full_source_text(self) -> None:
        self._insert(
            "m-structured",
            "结构化摘要：两类同学都要处理",
            source_text="原文完整内容：刚转专业同学需要注意，其他同学也要注意。" + "附件细节" * 300,
        )
        task_id = self.store.upsert_task(
            task_key="m-structured",
            summary="两类同学都要核查",
            audience="刚转专业同学；其他同学",
            condition="没有2026年体测成绩",
            details=["刚转专业同学先查看成绩", "其他同学也要查看成绩"],
            action="查看并预约",
            category="action",
            groups=["测试群"],
        )
        task = self.store.get_task(task_id)
        assert task is not None
        self.assertEqual(task["audience"], "刚转专业同学；其他同学")
        self.assertEqual(task["condition"], "没有2026年体测成绩")
        self.assertEqual(task["details"], ["刚转专业同学先查看成绩", "其他同学也要查看成绩"])
        self.assertIn("原文完整内容", task["source_text"])

    def test_task_actions_and_event_metrics(self) -> None:
        task_id = self.store.upsert_task(
            task_key="candidate-1",
            summary="可能要交报名表",
            category="action",
            groups=["班级群"],
            evidence="记得看一下报名表",
            status="candidate",
            confidence=0.61,
            source="qq_message",
            classification_reason="可能出现弱行动词",
        )
        candidate = self.store.list_tasks(statuses=("candidate",))[0]
        self.assertEqual(candidate["id"], task_id)
        self.assertEqual(candidate["confidence"], 0.61)

        self.assertTrue(self.store.apply_task_action(task_id, "confirm"))
        self.assertTrue(self.store.apply_task_action(task_id, "done"))
        self.assertTrue(self.store.apply_task_action(task_id, "reopen"))
        self.assertTrue(self.store.apply_task_action(task_id, "dismiss"))
        events = [item["event"] for item in self.store.list_task_events(task_id)]
        for expected in ("dismissed", "reopened", "done", "confirmed", "candidate_detected"):
            self.assertIn(expected, events)

        metrics = self.store.task_metrics(start="2020-01-01 00:00:00", end="2030-01-01 00:00:00")
        self.assertEqual(metrics["candidates"], 1)
        self.assertEqual(metrics["confirmed"], 1)
        self.assertEqual(metrics["dismissed"], 1)
        self.assertEqual(self.store.task_stats()["dismissed"], 1)


    def test_task_corrections_record_feedback_and_update_fields(self) -> None:
        urgent_id = self.store.upsert_task(
            task_key="urgent-1",
            summary="明早截止",
            category="urgent",
            importance=5,
        )
        self.assertTrue(self.store.apply_task_correction(urgent_id, "not_urgent"))
        urgent = self.store.get_task(urgent_id)
        assert urgent is not None
        self.assertEqual(urgent["category"], "action")
        self.assertEqual(urgent["importance"], 3)

        duplicate_id = self.store.upsert_task(
            task_key="duplicate-1",
            summary="重复通知",
            category="info",
        )
        self.assertTrue(self.store.apply_task_correction(duplicate_id, "duplicate"))
        duplicate = self.store.get_task(duplicate_id)
        assert duplicate is not None
        self.assertEqual(duplicate["status"], "dismissed")

        events = self.store.list_task_events(urgent_id)
        corrected = next(item for item in events if item["event"] == "corrected")
        self.assertIn("not_urgent", corrected["detail"])
        self.assertIn("previous", corrected["detail"])


    def test_snooze_stores_until_and_duplicate_links_original(self) -> None:
        original_id = self.store.upsert_task(
            task_key="m1",
            summary="提交实验报告",
            category="action",
            groups=["学院群"],
        )
        copy_id = self.store.upsert_task(
            task_key="m2",
            summary="提交实验报告",
            category="action",
            groups=["班级群"],
        )
        until = iso(dt.datetime(2026, 10, 7, 7, 30))
        self.assertTrue(self.store.apply_task_action(original_id, "snooze", detail={"until": until}))
        snoozed = self.store.get_task(original_id)
        assert snoozed is not None
        self.assertEqual(snoozed["snooze_until"], until)

        self.assertTrue(self.store.apply_task_correction(copy_id, "duplicate"))
        copy = self.store.get_task(copy_id)
        assert copy is not None
        self.assertEqual(copy["status"], "dismissed")
        self.assertEqual(copy["duplicate_of"], original_id)
        self.assertEqual(copy["duplicate_summary"], "提交实验报告")
        self.assertEqual(self.store.get_task(original_id)["groups"], ["学院群", "班级群"])

    def test_snooze_without_until_defaults_to_next_day(self) -> None:
        task_id = self.store.upsert_task(task_key="m1", summary="待办", category="action")
        self.assertTrue(self.store.apply_task_action(task_id, "snooze"))
        snoozed = self.store.get_task(task_id)
        assert snoozed is not None
        self.assertGreater(len(snoozed["snooze_until"]), 0)
        events = self.store.list_task_events(task_id)
        self.assertEqual(events[0]["event"], "snoozed")

    def test_correction_insights_summarise_group_mistakes(self) -> None:
        for index in range(3):
            task_id = self.store.upsert_task(
                task_key=f"u{index}",
                summary=f"紧急通知 {index}",
                category="urgent",
                groups=["新生交流群"],
            )
            self.store.apply_task_correction(task_id, "not_urgent")
        stats = self.store.correction_stats(days=30)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["by_type"]["not_urgent"], 3)
        self.assertEqual(stats["by_group"]["新生交流群"]["not_urgent"], 3)
        insights = self.store.correction_insights(days=30, min_group_hits=3)
        self.assertTrue(any("新生交流群" in text for text in insights))

    def test_delivery_stats_counts_statuses(self) -> None:
        digest_id = self.store.insert_digest(
            kind="window",
            window_start=iso(now_local()),
            window_end=iso(now_local()),
            body="正文",
            payload=[],
            message_count=1,
            llm_used=False,
        )
        should_send, delivery_id = self.store.claim_delivery(
            digest_id=digest_id,
            channel="webhook",
            target="t1",
            dedupe_key="k1",
            max_attempts=3,
            retry_seconds=0,
        )
        self.assertTrue(should_send)
        self.store.mark_delivery(delivery_id or 0, ok=True)
        stats = self.store.delivery_stats()
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(stats["pending"], 0)


class SearchEscapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "search.sqlite3")

    def _insert(self, msg_id: str, content: str) -> None:
        self.store.insert_message(
            msg_id=msg_id,
            group_id="g1",
            content=content,
            source_text="",
            sender_name="辅导员",
            group_name="学院群",
        )

    def test_percent_is_literal_not_wildcard(self) -> None:
        self._insert("m1", "进度 100% 完成")
        self._insert("m2", "无关通知")
        hits = self.store.search_messages("%")
        self.assertEqual([row["msg_id"] for row in hits], ["m1"])

    def test_underscore_is_literal(self) -> None:
        self._insert("m1", "文件 a_b 已上传")
        self._insert("m2", "文件 axb 已上传")
        hits = self.store.search_messages("a_b")
        self.assertEqual([row["msg_id"] for row in hits], ["m1"])

    def test_backslash_is_literal(self) -> None:
        self._insert("m1", r"路径 C:\临时 已建立")
        self._insert("m2", "路径 C:临时 已建立")
        hits = self.store.search_messages(r"C:\临时")
        self.assertEqual([row["msg_id"] for row in hits], ["m1"])


if __name__ == "__main__":
    unittest.main()
