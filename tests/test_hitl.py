"""低置信度人工确认与反馈回收（Roadmap A8）回归测试。

盯三件事：

1. 低置信度**一定**进待确认：阈值来自 ``QQ_DIGEST_CANDIDATE_MIN_CONFIDENCE``，
   不满足就不许直接进正式待办，且原因里要说清是多少分、阈值多少；
2. 确认 / 忽略 / 纠错都算反馈，能被汇总、被看见（``main.py feedback`` + ``doctor``）；
3. 反馈只汇总与建议，不自动改配置。

全程离线、用临时库，不碰真实 ``data/``。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402
from qq_digest import Message  # noqa: E402
from qq_live_digest import confidence, providers  # noqa: E402
from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.doctor import DoctorContext, check_confidence, check_feedback  # noqa: E402
from qq_live_digest.store import CORRECTION_LABELS, Store  # noqa: E402
from qq_live_digest.summarizer import classify_task_item  # noqa: E402

NOW = dt.datetime(2026, 10, 9, 12, 0, 0)
DEADLINE = dt.datetime(2026, 10, 9, 18, 0, 0)
ACTION_TEXT = "请各班班长今天18:00前提交材料"
CORRECTION_TYPES = (
    "not_notice", "not_task", "not_urgent", "duplicate", "category", "deadline", "clear_deadline",
)


def task_item(
    text: str = ACTION_TEXT,
    *,
    category: str = "action",
    score: int = 5,
    deadline: dt.datetime | None = None,
    action_words: tuple[str, ...] = ("提交",),
    evidence: str = ACTION_TEXT,
    tags: tuple[str, ...] = ("action",),
    group_id: str = "g1",
) -> dict:
    item = {
        "message": Message(timestamp=NOW, sender="辅导员", text=text, source="学院通知群"),
        "category": category,
        "score": score,
        "action_words": list(action_words),
        "evidence": evidence,
        "tags": list(tags),
        "group_id": group_id,
        "group_name": "学院通知群",
    }
    if deadline is not None:
        item["deadline_dt"] = deadline
    return item


class ThresholdTest(unittest.TestCase):
    def test_assess_task_uses_given_thresholds(self) -> None:
        item = task_item("提交材料", action_words=("提交",), evidence="提交材料")
        strict = confidence.assess_task(item, evidence="提交材料", high=0.95, low=0.9)
        self.assertEqual(strict.level, confidence.LEVEL_LOW)
        self.assertEqual(strict.details["confirm_threshold"], 0.9)
        loose = confidence.assess_task(item, evidence="提交材料", high=0.4, low=0.3)
        self.assertEqual(loose.level, confidence.LEVEL_HIGH)

    def test_confidence_value_does_not_depend_on_thresholds(self) -> None:
        item = task_item("提交材料", action_words=("提交",), evidence="提交材料")
        default = confidence.assess_task(item, evidence="提交材料")
        strict = confidence.assess_task(item, evidence="提交材料", high=0.95, low=0.9)
        self.assertEqual(default.confidence, strict.confidence)


class ClassifyGateTest(unittest.TestCase):
    def test_high_confidence_item_still_opens_directly(self) -> None:
        settings = Settings(group_whitelist=("g1",))
        result = classify_task_item(task_item(deadline=DEADLINE), settings)
        self.assertEqual(result["status"], "open")
        self.assertGreaterEqual(result["confidence"], settings.candidate_min_confidence)
        self.assertEqual(result["threshold"], settings.candidate_min_confidence)

    def test_low_confidence_item_is_demoted_to_candidate(self) -> None:
        strict = Settings(group_whitelist=("g1",), candidate_min_confidence=0.9)
        urgent = task_item(
            "马上提交材料", category="urgent", action_words=("提交",), evidence="马上提交材料"
        )
        result = classify_task_item(urgent, strict)
        self.assertEqual(result["status"], "candidate")
        self.assertIn("确认阈值", result["reason"])
        self.assertIn("90%", result["reason"])
        self.assertLess(result["confidence"], 0.9)

    def test_status_follows_configured_threshold(self) -> None:
        item = task_item(
            "马上提交材料", category="urgent", action_words=("提交",), evidence="马上提交材料"
        )
        loose = classify_task_item(
            item, Settings(group_whitelist=("g1",), candidate_min_confidence=0.2)
        )
        strict = classify_task_item(
            item, Settings(group_whitelist=("g1",), candidate_min_confidence=0.99)
        )
        self.assertEqual(loose["status"], "open")
        self.assertEqual(strict["status"], "candidate")
        # 只是分诊不同，把握度本身不变。
        self.assertEqual(strict["confidence"], loose["confidence"])


class FeedbackSummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "hitl.sqlite3")

    def test_every_correction_type_has_a_label(self) -> None:
        for kind in CORRECTION_TYPES:
            self.assertIn(kind, CORRECTION_LABELS)

    def test_empty_store_reports_no_feedback(self) -> None:
        summary = self.store.feedback_summary(days=30)
        self.assertEqual(summary["candidates"], 0)
        self.assertEqual(summary["confirmed"], 0)
        self.assertEqual(summary["dismissed"], 0)
        self.assertEqual(summary["corrected"], 0)
        self.assertEqual(summary["insights"], [])
        self.assertIn("window_start", summary)

    def test_confirm_dismiss_and_correct_are_collected(self) -> None:
        confirmed_id = self.store.upsert_task(
            task_key="c1",
            summary="确认提交材料",
            category="action",
            groups=["学院群"],
            status="candidate",
            confidence=0.61,
        )
        dismissed_id = self.store.upsert_task(
            task_key="c2",
            summary="闲聊被误判",
            category="action",
            groups=["学院群"],
            status="candidate",
            confidence=0.58,
        )
        fixed_id = self.store.upsert_task(
            task_key="c3", summary="明早截止", category="urgent", groups=["新生群"]
        )
        self.assertTrue(self.store.apply_task_action(confirmed_id, "confirm"))
        self.assertTrue(self.store.apply_task_action(dismissed_id, "dismiss"))
        self.assertTrue(self.store.apply_task_correction(fixed_id, "not_urgent"))

        summary = self.store.feedback_summary(days=30)
        self.assertEqual(summary["candidates"], 2)
        self.assertEqual(summary["confirmed"], 1)
        self.assertEqual(summary["dismissed"], 1)
        self.assertEqual(summary["corrected"], 1)
        self.assertEqual(summary["confirmation_rate"], 50.0)
        self.assertEqual(summary["dismissal_rate"], 50.0)
        self.assertEqual(summary["by_type"]["not_urgent"], 1)
        self.assertEqual(summary["by_group"]["新生群"]["not_urgent"], 1)


class FeedbackCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.env_file = self.data_dir / "test.env"
        self.env_file.write_text(
            f"QQ_DIGEST_DATA_DIR={self.data_dir.as_posix()}\n"
            f"QQ_DIGEST_LOG_DIR={(self.data_dir / 'logs').as_posix()}\n",
            encoding="utf-8",
        )
        self._saved = {
            key: os.environ.get(key) for key in ("QQ_DIGEST_DATA_DIR", "QQ_DIGEST_LOG_DIR")
        }
        self.addCleanup(self._restore_env)
        self.addCleanup(self._close_log_handlers)
        self.addCleanup(lambda: providers.set_call_recorder(None))
        self.store = Store(self.data_dir / "digest.sqlite3")

    def _restore_env(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _close_log_handlers(self) -> None:
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()

    def _run(self, *extra: str) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main.main(["--env", str(self.env_file), "feedback", *extra])
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_empty_state_is_explained(self) -> None:
        text = self._run()
        self.assertIn("还没有待确认候选", text)

    def test_populated_state_shows_rates_and_labels(self) -> None:
        confirmed_id = self.store.upsert_task(
            task_key="c1",
            summary="确认提交材料",
            category="action",
            groups=["学院群"],
            status="candidate",
            confidence=0.61,
        )
        fixed_id = self.store.upsert_task(
            task_key="c2", summary="明早截止", category="urgent", groups=["新生群"]
        )
        self.store.apply_task_action(confirmed_id, "confirm")
        self.store.apply_task_correction(fixed_id, "not_urgent")
        text = self._run("--days", "30")
        self.assertIn("候选 1 条", text)
        self.assertIn("确认 1", text)
        self.assertIn("误判紧急", text)
        self.assertIn("新生群", text)

    def test_json_output_is_machine_readable(self) -> None:
        payload = json.loads(self._run("--json"))
        self.assertIn("confirmation_rate", payload)
        self.assertIn("by_type", payload)
        self.assertIn("insights", payload)


class FeedbackDoctorCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.store = Store(self.data_dir / "digest.sqlite3")

    def _context(self, **overrides) -> DoctorContext:
        settings = Settings(data_dir=self.data_dir, **overrides)
        return DoctorContext(settings=settings, open_store=lambda: self.store)

    def test_no_candidates_is_a_plain_ok(self) -> None:
        check = check_feedback(self._context())
        self.assertEqual(check.status, "OK")
        self.assertIn("没有待确认候选", check.detail)

    def test_candidate_feedback_is_summarised(self) -> None:
        task_id = self.store.upsert_task(
            task_key="c1", summary="确认", category="action", status="candidate", confidence=0.61
        )
        self.store.apply_task_action(task_id, "confirm")
        check = check_feedback(self._context())
        self.assertEqual(check.status, "OK")
        self.assertIn("候选 1", check.detail)
        self.assertIn("确认 1", check.detail)

    def test_confidence_check_uses_configured_threshold(self) -> None:
        self.store.upsert_task(
            task_key="c1", summary="低置信", category="action", status="candidate", confidence=0.6
        )
        self.assertIn("建议人工确认 1", check_confidence(self._context(candidate_min_confidence=0.9)).detail)
        self.assertIn("建议人工确认 0", check_confidence(self._context(candidate_min_confidence=0.5)).detail)


if __name__ == "__main__":
    unittest.main()
