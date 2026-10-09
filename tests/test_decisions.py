"""决策日志（Roadmap A33）回归测试。

要守住的东西只有一句：**每条消息「为什么推 / 为什么不推」都能查得到，且原因与真实判定一致**。
所以这里既测结构（落库、查询、裁剪），也测语义（未命中给的是阈值原因、去重给的是对照文本）。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest import decisions  # noqa: E402
from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.push import Pusher  # noqa: E402
from qq_live_digest.service import DigestService  # noqa: E402
from qq_live_digest.store import Store  # noqa: E402
from qq_live_digest.summarizer import (  # noqa: E402
    analyse_records,
    build_digest,
    focus_reason,
    is_focus,
)
from qq_live_digest.timeutil import iso  # noqa: E402

NOW = dt.datetime(2026, 10, 8, 18, 0, 0)
NOTICE = "【学院通知】关于2026年国庆节放假安排的通知"
NOTICE_B = "【教务处】关于2026年秋季学期选课工作的通知"
CHATTER = "哈哈哈哈"


def make_record(
    msg_id: str,
    content: str,
    *,
    minutes_ago: int = 0,
    group: str = "g1",
    group_name: str = "学院通知群",
    sender: str = "辅导员",
) -> dict:
    stamp = NOW - dt.timedelta(minutes=minutes_ago)
    return {
        "msg_id": msg_id,
        "source": "qqbot",
        "event": "GROUP_MESSAGE_CREATE",
        "group_id": group,
        "group_name": group_name,
        "sender_id": "u1",
        "sender_name": sender,
        "ts": iso(stamp),
        "received_at": iso(stamp),
        "content": content,
    }


def outcomes_of(rows: list[dict]) -> dict[str, dict]:
    """msg_id → 该消息最新一条决策（查询按 id 倒序）。"""
    result: dict[str, dict] = {}
    for row in rows:
        result.setdefault(str(row.get("msg_id") or ""), row)
    return result


class FocusReasonTest(unittest.TestCase):
    """`focus_reason` 是判定本体，必须和 `is_focus` 永远一致。"""

    def setUp(self) -> None:
        self.settings = Settings(group_whitelist=("g1",), min_score=3)

    def _analysis(self, content: str, sender: str = "同学A") -> dict:
        return analyse_records([make_record("m1", content, sender=sender)])[0]

    def test_agrees_with_is_focus_on_every_sample(self) -> None:
        samples = [
            (NOTICE, "辅导员"),
            (CHATTER, "同学A"),
            ("有人一起去图书馆吗", "同学B"),
            ("收到", "同学A"),
            ("宿舍楼下贴了一张海报", "同学C"),
        ]
        for content, sender in samples:
            analysis = self._analysis(content, sender)
            reason = focus_reason(analysis, self.settings)
            with self.subTest(content=content):
                self.assertEqual(is_focus(analysis, self.settings), not reason)

    def test_notice_has_no_reason(self) -> None:
        # 分值里有「发信人可信度」一项，所以这里必须用辅导员这类权威发言人。
        analysis = self._analysis(NOTICE, "辅导员")
        self.assertEqual(focus_reason(analysis, self.settings), "")

    def test_chatter_reason_is_human_readable(self) -> None:
        analysis = self._analysis(CHATTER)
        self.assertTrue(focus_reason(analysis, self.settings))

    def test_low_score_reason_shows_both_numbers(self) -> None:
        analysis = self._analysis("宿舍楼下贴了一张海报", sender="同学C")
        reason = focus_reason(analysis, self.settings)
        if reason:
            self.assertIn(f"阈值 {self.settings.min_score}", reason)


class TrailTest(unittest.TestCase):
    """`build_digest` 产出的决策轨迹：谁进候选、谁被哪条规则挡下。"""

    def setUp(self) -> None:
        self.settings = Settings(
            group_whitelist=("g1",), group_aliases={"g1": "学院通知群"}, min_score=3
        )

    def test_selected_gets_pending_and_others_get_reasons(self) -> None:
        records = [make_record("m1", NOTICE), make_record("m2", CHATTER, sender="同学A")]
        digest = build_digest(self.settings, records, now=NOW)
        by_id = outcomes_of(digest.decisions)

        self.assertEqual(by_id["m1"]["outcome"], decisions.PENDING)
        self.assertEqual(by_id["m2"]["outcome"], decisions.FILTERED)
        self.assertTrue(by_id["m2"]["reason"])
        self.assertEqual(by_id["m2"]["min_score"], self.settings.min_score)

    def test_selected_records_score_and_rule_hits(self) -> None:
        digest = build_digest(self.settings, [make_record("m1", NOTICE)], now=NOW)
        row = outcomes_of(digest.decisions)["m1"]
        self.assertGreater(row["score"], 0)
        self.assertTrue(row["rule_hits"])
        self.assertIn(row["category"], {"urgent", "action", "academic", "info"})

    def test_in_batch_duplicate_is_deduped(self) -> None:
        records = [make_record("m1", NOTICE), make_record("m2", NOTICE)]
        digest = build_digest(self.settings, records, now=NOW)
        by_id = outcomes_of(digest.decisions)
        self.assertEqual(by_id["m1"]["outcome"], decisions.PENDING)
        self.assertEqual(by_id["m2"]["outcome"], decisions.DEDUPED)
        self.assertIn("本批", by_id["m2"]["reason"])

    def test_history_duplicate_keeps_the_reference_text(self) -> None:
        history = [
            {"text": NOTICE, "group": "学院通知群", "summary": "国庆放假安排", "ts": iso(NOW)}
        ]
        digest = build_digest(self.settings, [make_record("m1", NOTICE)], now=NOW, history=history)
        row = outcomes_of(digest.decisions)["m1"]
        self.assertEqual(row["outcome"], decisions.DEDUPED)
        self.assertIn("已推内容重复", row["reason"])
        self.assertIn("国庆放假安排", row["dedupe_reason"])

    def test_over_limit_items_are_truncated_not_silently_dropped(self) -> None:
        settings = Settings(
            group_whitelist=("g1",), group_aliases={"g1": "学院通知群"}, min_score=3, max_items=1
        )
        records = [make_record("m1", NOTICE), make_record("m2", NOTICE_B)]
        digest = build_digest(settings, records, now=NOW)
        by_id = outcomes_of(digest.decisions)
        outcomes = sorted(row["outcome"] for row in by_id.values())
        self.assertEqual(outcomes, [decisions.PENDING, decisions.TRUNCATED])
        truncated = next(row for row in by_id.values() if row["outcome"] == decisions.TRUNCATED)
        self.assertIn("上限 1", truncated["reason"])

    def test_every_negative_row_explains_itself(self) -> None:
        records = [
            make_record("m1", NOTICE),
            make_record("m2", CHATTER, sender="同学A"),
            make_record("m3", NOTICE),
        ]
        digest = build_digest(self.settings, records, now=NOW)
        for row in digest.decisions:
            with self.subTest(msg_id=row["msg_id"]):
                self.assertIn(row["outcome"], decisions.OUTCOMES)
                self.assertTrue(row["reason"] or row["outcome"] == decisions.PENDING)
                self.assertTrue(row["stage"])


class StoreDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "decisions.sqlite3")

    def test_round_trip_and_numeric_normalisation(self) -> None:
        written = self.store.record_decisions(
            [
                {
                    "msg_id": "m1",
                    "group_id": "g1",
                    "stage": decisions.STAGE_FILTER,
                    "outcome": decisions.FILTERED,
                    "reason": "分值 1 < 阈值 3",
                    "score": "1",
                    "min_score": None,
                    "rule_hits": ["chatter"],
                }
            ]
        )
        self.assertEqual(written, 1)
        row = self.store.decisions_for("m1")[0]
        self.assertEqual(row["score"], 1)
        self.assertEqual(row["min_score"], 0)
        self.assertEqual(json.loads(row["rule_hits"]), ["chatter"])
        self.assertEqual(row["stage"], decisions.STAGE_FILTER)

    def test_empty_and_junk_rows_are_ignored(self) -> None:
        self.assertEqual(self.store.record_decisions([{}, {"reason": "没有结论"}]), 0)
        self.assertEqual(self.store.counts()["decisions"], 0)

    def test_recent_decisions_filters_by_outcome(self) -> None:
        self.store.record_decisions(
            [
                {"msg_id": "m1", "outcome": decisions.PUSHED, "stage": decisions.STAGE_PUBLISH},
                {"msg_id": "m2", "outcome": decisions.FILTERED, "stage": decisions.STAGE_FILTER},
            ]
        )
        filtered = self.store.recent_decisions(outcome=decisions.FILTERED)
        self.assertEqual([row["msg_id"] for row in filtered], ["m2"])
        self.assertEqual(len(self.store.recent_decisions()), 2)

    def test_counts_and_prune_drop_old_rows(self) -> None:
        self.store.record_decisions(
            [
                {"msg_id": "new", "outcome": decisions.PUSHED},
                {
                    "msg_id": "old",
                    "outcome": decisions.PUSHED,
                    "created_at": iso(NOW - dt.timedelta(days=90)),
                },
            ]
        )
        self.assertEqual(self.store.decision_counts(hours=24 * 365)[decisions.PUSHED], 2)
        self.store.prune(retention_days=30)
        remaining = self.store.recent_decisions()
        self.assertEqual([row["msg_id"] for row in remaining], ["new"])

    def test_deferred_updates_in_place_instead_of_appending(self) -> None:
        """夜间静默一晚上要 tick 几百次，绝不能每次都插一行。"""
        base = {"msg_id": "m1", "group_id": "g1", "stage": decisions.STAGE_PUBLISH}
        self.assertEqual(self.store.record_deferred([{**base, "reason": "延后：夜间静默时段"}]), 1)
        self.assertEqual(
            self.store.record_deferred([{**base, "reason": "延后：当日推送额度已用完"}]), 1
        )
        rows = self.store.deferred_decisions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], decisions.DEFERRED)
        self.assertIn("额度", rows[0]["reason"])
        self.assertEqual(self.store.counts()["decisions_deferred"], 1)

    def test_final_decision_clears_the_deferred_row(self) -> None:
        """消息真正推出去之后，「延后未决」过程行必须消失，否则查出来自相矛盾。"""
        self.store.record_deferred([{"msg_id": "m1", "reason": "延后：夜间静默时段"}])
        self.assertEqual(self.store.deferred_count(), 1)
        self.store.record_decisions(
            [
                {
                    "msg_id": "m1",
                    "stage": decisions.STAGE_PUBLISH,
                    "outcome": decisions.PUSHED,
                    "reason": "命中候选，已推送",
                }
            ]
        )
        self.assertEqual(self.store.deferred_count(), 0)
        self.assertEqual([row["outcome"] for row in self.store.decisions_for("m1")], [decisions.PUSHED])

    def test_deferred_ignores_rows_without_msg_id(self) -> None:
        self.assertEqual(self.store.record_deferred([{}, {"reason": "没有消息号"}]), 0)
        self.assertEqual(self.store.deferred_count(), 0)


class _RecordingPusher(Pusher):
    name = "webhook"
    tier = 0

    def __init__(self) -> None:
        super().__init__("test-target")
        self.calls: list[str] = []

    def send(self, title: str, body: str, *, summary: str = "", html: str = "") -> None:
        self.calls.append(body)


class ServiceDecisionTest(unittest.TestCase):
    """端到端：入口拒绝、重复消息、推送与未命中都必须留下痕迹。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            data_dir=self.data_dir,
            window_minutes=10,
            min_score=3,
            delivery_retry_seconds=0,
        )
        self.store = Store(self.data_dir / "service.sqlite3")
        self.pusher = _RecordingPusher()
        self.service = DigestService(self.settings, store=self.store, pushers=[self.pusher])

    def test_pushed_message_is_recorded_with_digest_id(self) -> None:
        self.assertTrue(self.service.on_message(make_record("m1", NOTICE, minutes_ago=11)))
        self.service.tick(now=NOW)
        row = self.store.decisions_for("m1")[0]
        self.assertEqual(row["outcome"], decisions.PUSHED)
        self.assertGreater(row["digest_id"], 0)
        self.assertIn("已推送", row["reason"])

    def test_filtered_message_is_recorded_and_not_pushed(self) -> None:
        self.service.on_message(make_record("m1", CHATTER, minutes_ago=11, sender="同学A"))
        self.service.tick(now=NOW)
        self.assertEqual(self.pusher.calls, [])
        row = self.store.decisions_for("m1")[0]
        self.assertEqual(row["outcome"], decisions.FILTERED)
        self.assertTrue(row["reason"])

    def test_duplicate_msg_id_is_recorded(self) -> None:
        record = make_record("m1", NOTICE, minutes_ago=11)
        self.assertTrue(self.service.on_message(record))
        self.assertFalse(self.service.on_message(record))
        row = self.store.decisions_for("m1")[0]
        self.assertEqual(row["outcome"], decisions.DUPLICATE)
        self.assertEqual(row["stage"], decisions.STAGE_INTAKE)

    def test_missing_msg_id_is_recorded(self) -> None:
        record = make_record("m1", NOTICE)
        record.pop("msg_id")
        self.assertFalse(self.service.on_message(record))
        rows = self.store.recent_decisions(outcome=decisions.REJECTED)
        self.assertEqual(len(rows), 1)
        self.assertIn("msg_id", rows[0]["reason"])

    def test_whitelist_rejection_is_recorded_once_per_group_per_day(self) -> None:
        for index in range(3):
            self.assertFalse(
                self.service.on_message(
                    make_record(f"x{index}", NOTICE, group="g9", group_name="其他群")
                )
            )
        rows = self.store.recent_decisions(outcome=decisions.REJECTED)
        self.assertEqual(len(rows), 1)
        self.assertIn("白名单", rows[0]["reason"])
        self.assertIn("只记这一条", rows[0]["reason"])

    def test_no_channel_means_held_not_pushed(self) -> None:
        service = DigestService(self.settings, store=self.store, pushers=[])
        service.on_message(make_record("m1", NOTICE, minutes_ago=11))
        service.tick(now=NOW)
        row = self.store.decisions_for("m1")[0]
        self.assertEqual(row["outcome"], decisions.HELD)
        self.assertIn("未投出", row["reason"])

    def test_counts_expose_decisions_table(self) -> None:
        self.service.on_message(make_record("m1", NOTICE, minutes_ago=11))
        self.service.tick(now=NOW)
        self.assertGreaterEqual(self.store.counts()["decisions"], 1)

    def _quiet_service(self) -> DigestService:
        """静默时段覆盖 18:00，用来复现「夜里被挡住却什么都不留痕」那一段。"""
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            data_dir=self.data_dir,
            window_minutes=10,
            min_score=3,
            quiet_hours="17:00-19:00",
            catchup_enabled=False,
            delivery_retry_seconds=0,
        )
        return DigestService(settings, store=self.store, pushers=[self.pusher])

    def test_quiet_hours_records_deferred_instead_of_no_trace(self) -> None:
        service = self._quiet_service()
        service.on_message(make_record("m1", NOTICE, minutes_ago=11))
        service.tick(now=NOW)
        self.assertEqual(self.pusher.calls, [])
        rows = self.store.deferred_decisions(now=NOW)
        self.assertEqual([row["msg_id"] for row in rows], ["m1"])
        self.assertEqual(rows[0]["outcome"], decisions.DEFERRED)
        self.assertIn("延后", rows[0]["reason"])
        self.assertIn("夜间静默", rows[0]["reason"])
        self.assertEqual(self.store.counts()["decisions_deferred"], 1)

    def test_deferred_message_is_pushed_once_the_quiet_window_ends(self) -> None:
        service = self._quiet_service()
        service.on_message(make_record("m1", NOTICE, minutes_ago=11))
        service.tick(now=NOW)
        self.assertEqual(self.store.deferred_count(now=NOW), 1)
        service.tick(now=NOW + dt.timedelta(hours=1, minutes=5))
        self.assertEqual(len(self.pusher.calls), 1)
        self.assertEqual(self.store.deferred_count(), 0)
        self.assertEqual(self.store.decisions_for("m1")[0]["outcome"], decisions.PUSHED)

    def test_daily_budget_exhaustion_is_recorded_as_deferred(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            data_dir=self.data_dir,
            window_minutes=10,
            min_score=3,
            quiet_hours="",
            push_daily_budget=1,
            catchup_enabled=False,
            delivery_retry_seconds=0,
        )
        service = DigestService(settings, store=self.store, pushers=[self.pusher])
        self.store.meta_set(f"push_budget:{NOW:%Y-%m-%d}", "1")
        service.on_message(make_record("m1", NOTICE, minutes_ago=11))
        service.tick(now=NOW)
        self.assertEqual(self.pusher.calls, [])
        row = self.store.deferred_decisions(now=NOW)[0]
        self.assertEqual(row["outcome"], decisions.DEFERRED)
        self.assertIn("额度", row["reason"])


class RenderingTest(unittest.TestCase):
    """CLI 复用同一套渲染，保证命令行与落库口径一致。"""

    def test_labels_and_counts(self) -> None:
        self.assertEqual(decisions.outcome_label(decisions.FILTERED), "未命中")
        self.assertEqual(decisions.outcome_label("不存在的结论"), "不存在的结论")
        text = decisions.summarise_counts({decisions.PUSHED: 3, decisions.FILTERED: 1})
        self.assertIn("已推送 3", text)
        self.assertIn("未命中 1", text)
        self.assertLess(text.index("已推送"), text.index("未命中"))

    def test_describe_rows_expands_hits_and_reference(self) -> None:
        lines = decisions.describe_rows(
            [
                {
                    "created_at": iso(NOW),
                    "outcome": decisions.DEDUPED,
                    "reason": "与最近 6 小时内已推内容重复",
                    "score": 5,
                    "min_score": 3,
                    "category": "action",
                    "msg_id": "m1",
                    "digest_id": 7,
                    "rule_hits": json.dumps(["action", "行动词:报名"], ensure_ascii=False),
                    "dedupe_reason": "学院通知群 · 国庆放假安排",
                }
            ]
        )
        text = "\n".join(lines)
        self.assertIn("[重复跳过]", text)
        self.assertIn("分值 5/3", text)
        self.assertIn("摘要 #7", text)
        self.assertIn("行动词:报名", text)
        self.assertIn("学院通知群 · 国庆放假安排", text)


if __name__ == "__main__":
    unittest.main()
