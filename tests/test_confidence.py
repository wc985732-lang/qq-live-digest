"""置信度与可解释性（Roadmap A7）回归测试。

盯三件事：

1. 分数、等级、触发规则**同源**——把触发规则里的加减法加起来必须等于分数；
2. 归档 / 推送 / 待办台 / `main.py show` 看到的是同一套解释；
3. 没评估过的地方如实说没记录，不假装「0 分也算评估过」。

全程离线、纯函数，不碰真实 `data/`。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import logging
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402
import qq_digest  # noqa: E402
from qq_digest import Message  # noqa: E402
from qq_live_digest import confidence  # noqa: E402
from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.doctor import DoctorContext, check_confidence  # noqa: E402
from qq_live_digest.store import Store  # noqa: E402
from qq_live_digest.summarizer import (  # noqa: E402
    AMBIGUOUS_TIME_WORDS,
    STRONG_DIRECTIVE_WORDS,
    WEAK_ACTION_WORDS,
    Digest,
    build_digest,
    classify_task_item,
    evidence_sentence,
    format_push_text,
    html_push_text,
    payload_to_item,
)
from qq_live_digest.timeutil import iso  # noqa: E402

NOW = dt.datetime(2026, 10, 9, 12, 0, 0)
DELTA = re.compile(r"^([+-])(\d+\.\d{2}) ")

NOTICE_TEXT = "请各班班长今天18:00前提交材料，务必按时完成"
ACTION_TEXT = "请各班班长今天18:00前提交材料"
DEADLINE = dt.datetime(2026, 10, 9, 18, 0, 0)


def make_record(msg_id: str = "m1", content: str = NOTICE_TEXT) -> dict:
    return {
        "msg_id": msg_id,
        "source": "qqbot",
        "event": "GROUP_MESSAGE_CREATE",
        "group_id": "g1",
        "group_name": "学院通知群",
        "sender_id": "u1",
        "sender_name": "辅导员",
        "ts": iso(NOW),
        "received_at": iso(NOW),
        "content": content,
    }


def deltas(triggers) -> float:
    """把触发规则里的 `+0.18 …` / `-0.08 …` 加起来，验证「解释 == 分数」。"""
    total = 0.0
    for trigger in triggers:
        found = DELTA.match(str(trigger))
        if found:
            value = float(found.group(2))
            total += value if found.group(1) == "+" else -value
    return round(total, 2)


def expected_score(base: float, triggers) -> float:
    return round(min(0.98, max(0.05, base + deltas(triggers))), 2)


def expected_task_score(triggers) -> float:
    """待办尺度把基础分也写进了触发规则，所以直接对触发规则求和。"""
    return round(min(0.98, max(0.05, deltas(triggers))), 2)


def notice_item(text: str = NOTICE_TEXT, *, score: int | None = None, **extra) -> dict:
    message = Message(timestamp=NOW, sender="辅导员", text=text, source="学院通知群")
    item = qq_digest.analyze_message(message)
    item["group_id"] = "g1"
    item["group_name"] = "学院通知群"
    if score is not None:
        item["score"] = score
    item.update(extra)
    return item


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


def legacy_task_confidence(item: dict, quiet: bool = False) -> float:
    """A8 时期的算法（冻结版），用来证明 A7 只换了解释、没动数字。"""
    text = str(getattr(item.get("message"), "text", "") or "")
    category = str(item.get("category") or "info")
    action_words = [str(word) for word in (item.get("action_words") or []) if str(word)]
    deadline = item.get("deadline_dt") or item.get("deadline")
    has_deadline = isinstance(deadline, dt.datetime)
    evidence = str(item.get("evidence") or evidence_sentence(text, item))
    weak = next((word for word in WEAK_ACTION_WORDS if word in text), "")
    ambiguous = next((word for word in AMBIGUOUS_TIME_WORDS if word in text), "")
    strong_directive = any(word in text for word in STRONG_DIRECTIVE_WORDS)
    direct_action = bool(action_words) or any(
        word in text
        for word in (
            "提交", "填写", "报名", "缴费", "领取", "参加", "完成", "上传", "确认",
            "回复", "核对", "下载", "安装", "办理", "注册", "认证", "上交",
        )
    )
    score = int(item.get("score") or 0)
    value = 0.28
    if has_deadline:
        value += 0.25
    if strong_directive:
        value += 0.20
    if direct_action:
        value += 0.14
    if category == "urgent":
        value += 0.10
    if category == "action":
        value += 0.08
    if evidence:
        value += 0.05
    if score >= 4:
        value += 0.05
    if weak:
        value -= 0.24
    if ambiguous:
        value -= 0.14
    if quiet:
        value -= 0.08
    if not direct_action:
        value -= 0.08
    return round(max(0.05, min(0.98, value)), 2)


# ------------------------------------------------------------------ 等级与话术
class LevelTest(unittest.TestCase):
    def test_levels_follow_thresholds(self) -> None:
        self.assertEqual(confidence.level_of(0.75), confidence.LEVEL_HIGH)
        self.assertEqual(confidence.level_of(0.7499), confidence.LEVEL_MEDIUM)
        self.assertEqual(confidence.level_of(0.55), confidence.LEVEL_MEDIUM)
        self.assertEqual(confidence.level_of(0.5499), confidence.LEVEL_LOW)
        self.assertEqual(confidence.level_of(0.0), confidence.LEVEL_LOW)
        self.assertEqual(confidence.level_of(None), confidence.LEVEL_LOW)

    def test_levels_accept_custom_thresholds(self) -> None:
        self.assertEqual(confidence.level_of(0.6, high=0.6, low=0.5), confidence.LEVEL_HIGH)
        self.assertEqual(confidence.level_of(0.4, high=0.6, low=0.5), confidence.LEVEL_LOW)

    def test_level_labels_cover_all_levels(self) -> None:
        for level in confidence.LEVELS:
            self.assertTrue(confidence.level_label(level))
        self.assertEqual(confidence.level_label("weird"), "weird")

    def test_percent_rounds_to_whole_numbers(self) -> None:
        self.assertEqual(confidence.percent(0.82), 82)
        self.assertEqual(confidence.percent(0.5), 50)
        self.assertEqual(confidence.percent(None), 0)

    def test_confidence_text_uses_the_shared_wording(self) -> None:
        self.assertEqual(confidence.confidence_text(0.82), "把握较高 82%")
        self.assertEqual(confidence.confidence_text(0.66), "把握中等 66%")
        self.assertEqual(confidence.confidence_text(0.4), "把握较低 40%")


# ------------------------------------------------------------------ 触发规则拼句子
class TriggerSummaryTest(unittest.TestCase):
    def test_empty_triggers_have_no_text(self) -> None:
        self.assertEqual(confidence.describe_triggers([]), "")
        self.assertEqual(confidence.describe_triggers(["", "  "]), "")
        assessment = confidence.Assessment(confidence=0.3, level=confidence.LEVEL_LOW)
        self.assertEqual(assessment.reason, "没有命中任何规则")

    def test_joins_and_truncates(self) -> None:
        text = confidence.describe_triggers(["a", "b", "c", "d", "e"])
        self.assertEqual(text, "a；b；c；d；另有 1 条")
        self.assertEqual(confidence.describe_triggers(["a", "b"], limit=5), "a；b")

    def test_limit_one_keeps_the_strongest_rule(self) -> None:
        text = confidence.describe_triggers(["a", "", "b"], limit=1)
        self.assertEqual(text, "a；另有 1 条")

    def test_to_payload_is_json_shaped(self) -> None:
        assessment = confidence.assess_task(task_item(deadline=DEADLINE))
        payload = confidence.to_payload(assessment)
        self.assertEqual(payload["score"], assessment.confidence)
        self.assertEqual(payload["level"], assessment.level)
        self.assertEqual(payload["text"], confidence.confidence_text(assessment.confidence))
        self.assertEqual(payload["triggers"], list(assessment.triggers))
        self.assertEqual(payload["signals"], list(assessment.signals))
        self.assertEqual(payload["why"], confidence.describe_triggers(assessment.triggers))


# ------------------------------------------------------------------ 通知置信度
class NoticeAssessmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(group_whitelist=("g1",))

    def test_strong_notice_is_high_and_explains_every_bonus(self) -> None:
        item = notice_item()
        item["action"] = "提交材料"
        item["evidence"] = ACTION_TEXT
        result = confidence.assess_notice(item, self.settings, authoritative=True)
        self.assertEqual(result.level, confidence.LEVEL_HIGH)
        self.assertEqual(result.confidence, 0.98)
        for signal in (
            confidence.SIG_SCORE_MARGIN,
            confidence.SIG_WORD_HIT,
            confidence.SIG_DEADLINE,
            confidence.SIG_URGENT,
            confidence.SIG_ACTION,
            confidence.SIG_NOTICE_GROUP,
            confidence.SIG_EVIDENCE,
        ):
            self.assertIn(signal, result.signals)
        self.assertTrue(any(trigger.startswith("+0.18 分值") for trigger in result.triggers))
        self.assertTrue(any(trigger.startswith("+0.18 识别到截止时间") for trigger in result.triggers))

    def test_margin_of_one_gets_the_smaller_bonus(self) -> None:
        item = notice_item("群里发了一份课程表", score=4)
        result = confidence.assess_notice(item, self.settings)
        self.assertTrue(any(trigger.startswith("+0.10 分值 4") for trigger in result.triggers))
        self.assertFalse(any("两分以上" in trigger for trigger in result.triggers))

    def test_below_threshold_is_honest_about_zero_margin(self) -> None:
        item = notice_item("群里发了一份课程表", score=1)
        result = confidence.assess_notice(item, self.settings)
        self.assertTrue(any(trigger.startswith("+0.00 分值 1") for trigger in result.triggers))
        self.assertNotIn(confidence.SIG_SCORE_MARGIN, result.signals)

    def test_classification_bonuses_move_the_score(self) -> None:
        item = notice_item("群里发了一份课程表", score=3)
        plain = confidence.assess_notice(item, self.settings)
        boosted = confidence.assess_notice(item, self.settings, authoritative=True, llm_used=True)
        self.assertIn(confidence.SIG_NOTICE_GROUP, boosted.signals)
        self.assertIn(confidence.SIG_LLM, boosted.signals)
        self.assertEqual(boosted.confidence, round(plain.confidence + 0.10, 2))

    def test_quiet_group_and_information_broadcast_are_penalised(self) -> None:
        item = notice_item("图书馆周末正常开放", score=3)
        item["quiet_group"] = True
        result = confidence.assess_notice(item, self.settings)
        self.assertIn(confidence.SIG_QUIET_GROUP, result.signals)
        self.assertIn(confidence.SIG_NO_ACTION, result.signals)
        self.assertTrue(any(trigger.startswith("-0.10") for trigger in result.triggers))
        self.assertTrue(any(trigger.startswith("-0.08") for trigger in result.triggers))

    def test_triggers_add_up_to_the_score(self) -> None:
        for item in (
            notice_item(),
            notice_item("群里发了一份课程表", score=1),
            notice_item("图书馆周末正常开放", score=3),
        ):
            result = confidence.assess_notice(item, self.settings)
            self.assertEqual(result.confidence, expected_score(0.30, result.triggers))

    def test_details_keep_the_inputs_for_debugging(self) -> None:
        result = confidence.assess_notice(notice_item(), self.settings)
        self.assertEqual(result.details["score"], result.details["score"])
        self.assertEqual(result.details["min_score"], 3)
        self.assertTrue(result.details["has_deadline"])
        self.assertFalse(result.details["quiet"])


# ------------------------------------------------------------------ 待办置信度
class TaskAssessmentTest(unittest.TestCase):
    def test_base_case_only(self) -> None:
        item = task_item("群里发了一份课程表", category="info", score=2, action_words=(), evidence="")
        result = confidence.assess_task(item)
        self.assertEqual(result.confidence, 0.2)
        self.assertEqual(result.level, confidence.LEVEL_LOW)
        self.assertEqual(result.triggers[0], "+0.28 基础分：像一条待办")
        self.assertIn(confidence.SIG_NO_ACTION, result.signals)

    def test_full_task_hits_every_bonus(self) -> None:
        result = confidence.assess_task(task_item(deadline=DEADLINE))
        self.assertEqual(result.confidence, 0.98)
        self.assertEqual(result.level, confidence.LEVEL_HIGH)
        for signal in (
            confidence.SIG_DEADLINE,
            confidence.SIG_DIRECTIVE,
            confidence.SIG_ACTION,
            confidence.SIG_EVIDENCE,
            confidence.SIG_SCORE_MARGIN,
        ):
            self.assertIn(signal, result.signals)

    def test_urgent_and_action_categories_get_their_bonus(self) -> None:
        action = confidence.assess_task(task_item())
        urgent = confidence.assess_task(task_item(category="urgent", tags=("urgent",)))
        self.assertIn(confidence.SIG_URGENT, urgent.signals)
        self.assertTrue(any(trigger.startswith("+0.08 归类为行动项") for trigger in action.triggers))
        self.assertEqual(urgent.confidence, round(action.confidence + 0.10 - 0.08, 2))

    def test_weak_and_ambiguous_wording_penalise(self) -> None:
        weak = confidence.assess_task(task_item("记得有空提交材料", deadline=DEADLINE))
        ambiguous = confidence.assess_task(task_item("请尽快提交材料", deadline=DEADLINE))
        self.assertIn(confidence.SIG_WEAK_WORDING, weak.signals)
        self.assertIn(confidence.SIG_AMBIGUOUS_TIME, ambiguous.signals)
        self.assertTrue(any(trigger.startswith("-0.24") for trigger in weak.triggers))
        self.assertTrue(any(trigger.startswith("-0.14") for trigger in ambiguous.triggers))

    def test_quiet_group_penalty(self) -> None:
        item = task_item(deadline=None, action_words=("提交",))
        loud = confidence.assess_task(item)
        quiet = confidence.assess_task(item, quiet=True)
        self.assertIn(confidence.SIG_QUIET_GROUP, quiet.signals)
        self.assertEqual(quiet.confidence, max(0.05, round(loud.confidence - 0.08, 2)))

    def test_evidence_override_is_respected(self) -> None:
        item = task_item(deadline=None, text="请提交材料", evidence="")
        with_evidence = confidence.assess_task(item, evidence="请各班班长提交材料")
        without = confidence.assess_task(item, evidence="")
        self.assertEqual(with_evidence.confidence, round(without.confidence + 0.05, 2))
        self.assertIn(confidence.SIG_EVIDENCE, with_evidence.signals)
        self.assertNotIn(confidence.SIG_EVIDENCE, without.signals)

    def test_triggers_add_up_to_the_score(self) -> None:
        for item in (
            task_item(),
            task_item("记得有空提交材料", deadline=DEADLINE),
            task_item("请尽快提交材料", deadline=DEADLINE),
            task_item("群里发了一份课程表", category="info", score=2, action_words=(), evidence=""),
        ):
            result = confidence.assess_task(item)
            self.assertEqual(result.confidence, expected_task_score(result.triggers))

    def test_matches_legacy_formula(self) -> None:
        settings = Settings(group_whitelist=("g1",))
        items = [
            task_item(),
            task_item(deadline=DEADLINE),
            task_item(category="urgent", tags=("urgent",), deadline=DEADLINE),
            task_item("记得有空提交材料"),
            task_item("请尽快提交材料", deadline=DEADLINE),
            task_item("下周一开班会", category="info", score=3, action_words=(), evidence=""),
            task_item("群里发了一份课程表", category="info", score=2, action_words=(), evidence=""),
            task_item(evidence=""),
        ]
        for item in items:
            for quiet in (False, True):
                expected = legacy_task_confidence(item, quiet)
                actual = confidence.assess_task(
                    item,
                    quiet=quiet,
                    evidence=str(item.get("evidence") or evidence_sentence(str(item["message"].text), item)),
                ).confidence
                self.assertEqual(actual, expected, f"{item['message'].text} quiet={quiet}")
                if not quiet:
                    self.assertEqual(classify_task_item(item, settings)["confidence"], expected)


# ------------------------------------------------------------------ 分类接线
class ClassifyTaskItemTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(group_whitelist=("g1",))

    def test_confidence_comes_from_the_shared_assessment(self) -> None:
        for item in (task_item(), task_item("记得有空提交材料"), task_item(deadline=DEADLINE)):
            result = classify_task_item(item, self.settings)
            evidence = str(item.get("evidence") or evidence_sentence(str(item["message"].text), item))
            expected = confidence.assess_task(item, evidence=evidence)
            self.assertEqual(result["confidence"], expected.confidence)

    def test_level_and_triggers_are_exposed(self) -> None:
        result = classify_task_item(task_item(deadline=DEADLINE), self.settings)
        self.assertIn(result["level"], confidence.LEVELS)
        self.assertTrue(result["triggers"])
        self.assertTrue(all(isinstance(trigger, str) for trigger in result["triggers"]))

    def test_status_and_reason_text_are_unchanged(self) -> None:
        weak = classify_task_item(task_item("记得有空提交材料", deadline=DEADLINE), self.settings)
        self.assertEqual(weak["status"], "candidate")
        self.assertIn("记得", weak["reason"])
        opened = classify_task_item(task_item(deadline=DEADLINE), self.settings)
        self.assertEqual(opened["status"], "open")
        self.assertIn("截止时间", opened["reason"])


# ------------------------------------------------------------------ 归档 / 推送同一套解释
class PayloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(group_whitelist=("g1",), llm_enabled=False)

    def test_payload_carries_confidence_and_why(self) -> None:
        digest = build_digest(self.settings, [make_record()], now=NOW)
        payload = digest.payload()[0]
        info = payload["confidence"]
        self.assertGreater(info["score"], 0.5)
        self.assertIn(info["level"], confidence.LEVELS)
        self.assertTrue(info["triggers"])
        self.assertIn(confidence.SIG_DEADLINE, info["signals"])
        self.assertTrue(payload["why"])

    def test_payload_keeps_the_why_consistent_with_the_score(self) -> None:
        digest = build_digest(self.settings, [make_record()], now=NOW)
        info = digest.payload()[0]["confidence"]
        self.assertEqual(info["why"], confidence.describe_triggers(info["triggers"]))
        self.assertEqual(info["text"], confidence.confidence_text(info["score"]))

    def test_payload_to_item_restores_confidence(self) -> None:
        digest = build_digest(self.settings, [make_record()], now=NOW)
        entry = dict(digest.payload()[0], created_at=iso(NOW))
        item = payload_to_item(entry)
        self.assertEqual(item["confidence"]["level"], entry["confidence"]["level"])
        self.assertEqual(item["why"], entry["why"])

    def test_push_text_explains_why(self) -> None:
        digest = build_digest(self.settings, [make_record()], now=NOW)
        body = format_push_text(digest, self.settings)
        self.assertIn("为什么：", body)
        self.assertIn("把握", body)

    def test_html_push_text_explains_why(self) -> None:
        digest = build_digest(self.settings, [make_record()], now=NOW)
        html = html_push_text(digest, self.settings)
        self.assertIn("为什么：", html)
        self.assertIn("把握", html)

    def test_manual_digest_without_confidence_stays_empty(self) -> None:
        item = {
            "message": Message(timestamp=NOW, sender="同学", text="今晚开班会", source="班级群"),
            "category": "info",
            "score": 3,
            "evidence": "今晚开班会",
            "summary": "今晚开班会",
            "action": "",
        }
        digest = Digest(kind="window", window_start=NOW, window_end=NOW, items=[item])
        payload = digest.payload()[0]
        self.assertEqual(payload["confidence"], {})
        self.assertEqual(payload["why"], "")
        self.assertNotIn("为什么：", format_push_text(digest, self.settings))


# ------------------------------------------------------------------ CLI
class ShowCommandTest(unittest.TestCase):
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

    def _archive(self, payload: list[dict]) -> None:
        self.store.insert_digest(
            kind="window",
            window_start=iso(NOW),
            window_end=iso(NOW),
            body="正文",
            payload=payload,
            message_count=len(payload),
            llm_used=False,
        )

    def _run_show(self, *extra: str) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main.main(["--env", str(self.env_file), "show", *extra])
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_show_prints_confidence_and_why(self) -> None:
        settings = Settings(group_whitelist=("g1",), llm_enabled=False)
        digest = build_digest(settings, [make_record()], now=NOW)
        self._archive(digest.payload())
        text = self._run_show()
        self.assertIn("为什么：", text)
        self.assertIn("把握", text)

    def test_show_admits_missing_record(self) -> None:
        self._archive(
            [
                {
                    "category": "info",
                    "importance": 3,
                    "score": 3,
                    "summary": "旧摘要",
                    "audience": "",
                    "condition": "",
                    "details": [],
                    "action": "",
                    "evidence": "旧摘要",
                    "deadline": "",
                    "group": "某群",
                    "group_id": "g1",
                    "sender": "某人",
                    "text": "旧摘要",
                    "msg_id": "old1",
                }
            ]
        )
        text = self._run_show()
        self.assertIn("没有置信度记录", text)
        self.assertNotIn("把握", text)


# ------------------------------------------------------------------ 待办台候选卡片
class CandidateCardTest(unittest.TestCase):
    def test_candidate_card_reports_the_same_why(self) -> None:
        task = {
            "id": 1,
            "status": "candidate",
            "confidence": 0.66,
            "summary": "可能要交表",
            "deadline": "",
            "snooze_until": "",
            "candidate_detail": json.dumps(
                {
                    "confidence": 0.66,
                    "reason": "原话有“可能”等不确定措辞",
                    "level": confidence.LEVEL_MEDIUM,
                    "triggers": ["+0.28 基础分：像一条待办", "+0.14 有可执行动作：提交"],
                },
                ensure_ascii=False,
            ),
        }
        card = group_tasks([task], NOW)["candidates"][0]
        self.assertEqual(card["confidence_text"], "把握中等 66%")
        self.assertIn("+0.28 基础分", card["confidence_why"])
        self.assertIn("不确定措辞", card["confidence_reason"])


# ------------------------------------------------------------------ doctor
class DoctorConfidenceCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.store = Store(self.data_dir / "digest.sqlite3")

    def _context(self) -> DoctorContext:
        return DoctorContext(
            settings=Settings(data_dir=self.data_dir), open_store=lambda: self.store
        )

    def test_no_candidates_is_a_plain_ok(self) -> None:
        check = check_confidence(self._context())
        self.assertEqual(check.status, "OK")
        self.assertIn("没有待确认候选", check.detail)

    def test_candidates_count_levels_and_hint(self) -> None:
        self.store.upsert_task(task_key="c1", summary="可能要交表", status="candidate", confidence=0.4)
        self.store.upsert_task(task_key="c2", summary="交材料", status="candidate", confidence=0.9)
        check = check_confidence(self._context())
        self.assertEqual(check.status, "OK")
        self.assertIn("待确认 2 条", check.detail)
        self.assertIn("把握较高 1", check.detail)
        self.assertIn("建议人工确认 1", check.detail)
        self.assertIn("待办台", check.hint)


if __name__ == "__main__":
    unittest.main()
from qq_live_digest.webapp import group_tasks  # noqa: E402
