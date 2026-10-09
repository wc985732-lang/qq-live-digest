"""事件级跨群聚合（Roadmap A11）回归测试。

守住三件事：

1. 结构化事件键（对象 + 动作 + 时间 + 截止 + 来源）能把「换个说法的同一条事」合起来，
   又不会把「同一天的两件不同事」误合；
2. 合并沿用既有 duplicate_groups 口径，并在决策日志里留下 MERGED 痕迹；
3. 人工拆分覆盖真的能挡住自动合并（误合并可拆分）。
"""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_digest import Message  # noqa: E402
from qq_live_digest import decisions, events  # noqa: E402
from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.push import Pusher  # noqa: E402
from qq_live_digest.service import DigestService  # noqa: E402
from qq_live_digest.store import Store  # noqa: E402
from qq_live_digest.timeutil import iso  # noqa: E402

NOW = dt.datetime(2026, 10, 8, 18, 0, 0)
SIGNUP_A = "【学院通知】关于2026年10月15日创新创业大赛报名的通知，请各班于10月15日前提交材料"
SIGNUP_B = "【学院】10月15日创新创业大赛报名，请各班提交材料"
EXAM = "【教务处】关于10月16日大学英语四六级考试报名工作的通知"


def make_item(
    msg_id: str,
    text: str,
    group: str,
    *,
    score: int = 6,
    category: str = "task",
    action_words=(),
    deadline=None,
) -> dict:
    return {
        "msg_id": msg_id,
        "message": Message(timestamp=NOW, sender="辅导员", text=text, source=group),
        "group_name": group,
        "group_id": group,
        "score": score,
        "category": category,
        "action_words": list(action_words),
        "deadline": deadline,
    }


class EventKeyTest(unittest.TestCase):
    def test_source_header_is_normalised(self):
        self.assertEqual(events.source_tokens("【学院通知】正文"), ("学院",))
        self.assertEqual(events.source_tokens("【教务处】正文"), ("教务处",))
        self.assertEqual(events.source_tokens("【重要】正文"), ())

    def test_time_tokens_cover_dates_and_clock(self):
        tokens = events.time_tokens("10月15日 18:00 交材料，本周五截止")
        self.assertIn("10月15日", tokens)
        self.assertIn("18:00", tokens)

    def test_event_key_is_stable_and_has_five_parts(self):
        item = make_item(
            "m1", SIGNUP_A, "学院通知群", action_words=["报名"], deadline=dt.datetime(2026, 10, 15)
        )
        key = events.event_key(item)
        self.assertEqual(key, events.event_key(item))
        self.assertEqual(len(key.split("|")), 5)
        self.assertIsNotNone(events.signature_from_key(key))
        self.assertIsNone(events.signature_from_key("broken-key"))

    def test_compatible_merges_reworded_notice(self):
        first = events.event_parts(
            make_item("m1", SIGNUP_A, "学院通知群", action_words=["报名"], deadline=dt.datetime(2026, 10, 15))
        )
        second = events.event_parts(
            make_item("m2", SIGNUP_B, "计科1班", action_words=["报名"], deadline=dt.datetime(2026, 10, 15))
        )
        self.assertTrue(events.compatible(first, second))

    def test_compatible_rejects_other_event(self):
        first = events.event_parts(
            make_item("m1", SIGNUP_A, "学院通知群", action_words=["报名"], deadline=dt.datetime(2026, 10, 15))
        )
        other = events.event_parts(
            make_item("m3", EXAM, "教务群", action_words=["报名"], deadline=dt.datetime(2026, 10, 16))
        )
        self.assertFalse(events.compatible(first, other))
        self.assertFalse(events.compatible(first, events.event_parts(make_item("m4", "哈哈哈哈", "闲聊群"))))

    def test_compatible_needs_more_overlap_without_anchor(self):
        left = {"action": "", "time": (), "deadline": "", "source": (), "objects": {"组会", "通知", "报名"}}
        right = {"action": "", "time": (), "deadline": "", "source": (), "objects": {"组会", "通知", "报名"}}
        # 3 个共享对象词且无日期/来源/截止锚点：旧逻辑会误合，现在要求至少 4 个
        self.assertFalse(events.compatible(left, right))
        left4 = dict(left, objects={"组会", "通知", "报名", "签到"})
        right4 = dict(right, objects={"组会", "通知", "报名", "签到"})
        self.assertTrue(events.compatible(left4, right4))


class MergeTest(unittest.TestCase):
    def test_merge_keeps_highest_score_and_records_sources(self):
        primary = make_item(
            "m1", SIGNUP_A, "学院通知群", score=9, action_words=["报名"], deadline=dt.datetime(2026, 10, 15)
        )
        duplicate = make_item(
            "m2", SIGNUP_B, "计科1班", score=5, action_words=["报名"], deadline=dt.datetime(2026, 10, 15)
        )
        other = make_item(
            "m3", EXAM, "教务群", score=7, action_words=["报名"], deadline=dt.datetime(2026, 10, 16)
        )
        kept, merged = events.merge_items([duplicate, other, primary])
        self.assertEqual([item["msg_id"] for item in kept], ["m1", "m3"])
        self.assertEqual([record["item"]["msg_id"] for record in merged], ["m2"])
        self.assertEqual(kept[0]["duplicate_groups"], ["学院通知群", "计科1班"])
        self.assertTrue(kept[0]["event_merged"])
        self.assertEqual(merged[0]["into"]["msg_id"], "m1")

    def test_merge_is_order_independent(self):
        first = make_item("m1", SIGNUP_A, "学院通知群", score=5, action_words=["报名"], deadline=dt.datetime(2026, 10, 15))
        second = make_item("m2", SIGNUP_B, "计科1班", score=5, action_words=["报名"], deadline=dt.datetime(2026, 10, 15))
        kept_one, merged_one = events.merge_items([first, second])
        kept_two, merged_two = events.merge_items([second, first])
        self.assertEqual(len(kept_one), len(kept_two))
        self.assertEqual(len(merged_one), len(merged_two))
        self.assertEqual(kept_one[0]["message"].text, kept_two[0]["message"].text)

    def test_split_coverage_blocks_merge(self):
        first = make_item("m1", SIGNUP_A, "学院通知群", action_words=["报名"], deadline=dt.datetime(2026, 10, 15))
        second = make_item("m2", SIGNUP_B, "计科1班", action_words=["报名"], deadline=dt.datetime(2026, 10, 15))
        kept, merged = events.merge_items([first, second], split_keys=[events.event_key(second)])
        self.assertEqual(len(kept), 2)
        self.assertEqual(merged, [])


class _RecordingPusher(Pusher):
    name = "webhook"
    tier = 0

    def __init__(self) -> None:
        super().__init__("test-target")
        self.calls: list[str] = []

    def send(self, title: str, body: str, *, summary: str = "", html: str = "") -> None:
        self.calls.append(body)


class EventPipelineTest(unittest.TestCase):
    """端到端：开关打开时跨群同一事件合成一条，并在决策日志里留痕。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.settings = Settings(
            group_whitelist=("g1", "g2"),
            group_aliases={"g1": "学院通知群", "g2": "计科1班"},
            data_dir=self.data_dir,
            window_minutes=10,
            min_score=3,
            dedupe_hours=0,
            delivery_retry_seconds=0,
            event_merge=True,
        )
        self.store = Store(self.data_dir / "events.sqlite3")
        self.pusher = _RecordingPusher()
        self.service = DigestService(self.settings, store=self.store, pushers=[self.pusher])

    def _record(self, msg_id: str, content: str, *, group: str, group_name: str) -> dict:
        stamp = NOW - dt.timedelta(minutes=11)
        return {
            "msg_id": msg_id,
            "source": "qqbot",
            "event": "GROUP_MESSAGE_CREATE",
            "group_id": group,
            "group_name": group_name,
            "sender_id": "u1",
            "sender_name": "辅导员",
            "ts": iso(stamp),
            "received_at": iso(stamp),
            "content": content,
        }

    def _digest(self) -> dict:
        self.service.tick(now=NOW)
        digests = self.store.recent_digests(limit=1)
        self.assertTrue(digests, "应该产出一条摘要")
        return digests[0]

    def test_reworded_cross_group_notice_is_merged(self):
        self.service.on_message(self._record("m1", SIGNUP_A, group="g1", group_name="学院通知群"))
        self.service.on_message(self._record("m2", SIGNUP_B, group="g2", group_name="计科1班"))
        digest = self._digest()
        self.assertTrue(digest["items"])
        primary = digest["items"][0]
        self.assertTrue(primary["event_merged"])
        self.assertEqual(set(primary["duplicate_groups"]), {"学院通知群", "计科1班"})
        merged_rows = self.store.recent_decisions(outcome=decisions.MERGED, limit=10)
        self.assertEqual([row["msg_id"] for row in merged_rows], ["m2"])
        self.assertEqual(merged_rows[0]["stage"], decisions.STAGE_EVENT)

    def test_disabled_switch_keeps_items_separate(self):
        self.settings.event_merge = False
        self.service.on_message(self._record("m1", SIGNUP_A, group="g1", group_name="学院通知群"))
        self.service.on_message(self._record("m2", SIGNUP_B, group="g2", group_name="计科1班"))
        digest = self._digest()
        self.assertFalse(any(item.get("event_merged") for item in digest["items"]))
        self.assertEqual(self.store.recent_decisions(outcome=decisions.MERGED, limit=10), [])

    def test_split_coverage_is_recorded_and_honoured(self):
        self.service.on_message(self._record("m1", SIGNUP_A, group="g1", group_name="学院通知群"))
        self.service.on_message(self._record("m2", SIGNUP_B, group="g2", group_name="计科1班"))
        self._digest()  # 先合并一次，拿到事件键
        merged_row = self.store.recent_decisions(outcome=decisions.MERGED, limit=1)[0]
        key = merged_row["dedupe_reason"].replace("event ", "").strip()
        self.assertTrue(self.store.add_event_split(key, reason="确认是两件事"))
        self.assertEqual(self.store.event_split_keys(), (key,))
        reopened = Store(self.data_dir / "events.sqlite3")  # 换一个实例也要读得到，确认落库
        self.assertEqual(reopened.event_split_keys(), (key,))
        self.service.store = reopened
        self.service.on_message(self._record("m3", SIGNUP_A, group="g1", group_name="学院通知群"))
        self.service.on_message(self._record("m4", SIGNUP_B, group="g2", group_name="计科1班"))
        digest = self._digest()
        self.assertFalse(any(item.get("event_merged") for item in digest["items"]))


if __name__ == "__main__":
    unittest.main()
