"""群级个性化策略（Roadmap A9）回归测试。

盯四件事：

1. 解析层对坏配置足够宽容：坏 JSON / 坏字段 / 坏时段只丢自己，不抛异常；
2. 「没写的字段继承全局」是真的——只改一个群不会牵连别的群，老配置
   `QQ_DIGEST_QUIET_GROUPS` 也仍然生效；
3. 策略真的接到判定上：群关键词命中算明确通知、免打扰按时段内的消息时间算、
   本群最低分覆盖全局阈值、模型档只对单群批次生效；
4. `main.py groups` 与 doctor 能把每个群最终生效的开关讲清楚。

全程离线、纯函数，不碰真实 `data/`。
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
from qq_live_digest import grouppolicy, llmstats, summarizer  # noqa: E402
from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.doctor import OK, DoctorContext, check_groups  # noqa: E402
from qq_live_digest.routing import decide_route  # noqa: E402

NOW = dt.datetime(2026, 10, 9, 12, 0, 0)
ENV_KEYS = (
    "QQ_DIGEST_DATA_DIR",
    "QQ_DIGEST_LOG_DIR",
    "QQ_DIGEST_GROUPS",
    "QQ_DIGEST_GROUP_ALIASES",
    "QQ_DIGEST_GROUP_POLICIES",
    "QQ_DIGEST_QUIET_GROUPS",
    "QQ_DIGEST_MIN_SCORE",
)


def policy_settings(**overrides) -> Settings:
    values = dict(group_whitelist=("g1", "g2"), min_score=3)
    values.update(overrides)
    return Settings(**values)


def route_settings(**overrides) -> Settings:
    values = dict(
        group_whitelist=("g1",),
        group_aliases={"g1": "学院通知群"},
        min_score=3,
        dashscope_model="qwen-plus",
        dashscope_api_key="test-key",
        llm_model_light="qwen-turbo",
        llm_enabled=True,
    )
    values.update(overrides)
    return Settings(**values)


def item(
    text: str = "大家晚上好",
    *,
    group_id: str = "g1",
    name: str = "学院通知群",
    moment: dt.datetime = NOW,
    score: int = 0,
    category: str = "info",
) -> dict:
    return {
        "message": Message(timestamp=moment, sender="同学", text=text, source=name),
        "group_id": group_id,
        "group_name": name,
        "score": score,
        "category": category,
    }


def high_item() -> dict:
    """高置信度候选：够得上路由判据，也有群信息（用来验模型档 pin）。"""
    return {
        "group_id": "g1",
        "group_name": "学院通知群",
        "score": 6,
        "deadline_dt": NOW + dt.timedelta(hours=20),
        "action": "提交材料",
        "urgent": ["今天"],
        "action_words": ["提交"],
        "evidence": "请各班于明天 12:00 前提交材料",
    }


class ParseHelpersTest(unittest.TestCase):
    def test_parse_bool_accepts_chinese_and_english_words(self):
        for value in (True, "true", "1", "是", "开", 1):
            self.assertIs(grouppolicy.parse_bool(value), True)
        for value in (False, "false", "0", "否", "关", 0):
            self.assertIs(grouppolicy.parse_bool(value), False)

    def test_parse_bool_falls_back_when_unreadable(self):
        self.assertIsNone(grouppolicy.parse_bool("也许"))
        self.assertTrue(grouppolicy.parse_bool("也许", default=True))

    def test_parse_keywords_splits_on_common_separators(self):
        self.assertEqual(
            grouppolicy.parse_keywords("考试,选课、开会;值班 报名"),
            ("考试", "选课", "开会", "值班", "报名"),
        )
        self.assertEqual(grouppolicy.parse_keywords(["考试", "考试", "选课"]), ("考试", "选课"))
        self.assertEqual(grouppolicy.parse_keywords(None), ())

    def test_parse_clock_reads_cross_midnight_and_rejects_bad_input(self):
        self.assertEqual(grouppolicy.parse_clock("22:00-07:00"), (1320, 420))
        self.assertEqual(grouppolicy.parse_clock("23:00~06:30"), (1380, 390))
        self.assertIsNone(grouppolicy.parse_clock("09:00-09:00"))
        self.assertIsNone(grouppolicy.parse_clock("25:00-07:00"))
        self.assertIsNone(grouppolicy.parse_clock("22:70-07:00"))
        self.assertIsNone(grouppolicy.parse_clock("晚上十点"))

    def test_normalize_hours_pads_and_drops_invalid(self):
        self.assertEqual(grouppolicy.normalize_hours("7:0-8:00"), "07:00-08:00")
        self.assertEqual(grouppolicy.normalize_hours("oops"), "")

    def test_normalize_entry_keeps_known_fields_only(self):
        entry = grouppolicy.normalize_entry(
            {
                "quiet": "是",
                "keywords": "考试",
                "min_score": "5",
                "model": "LIGHT",
                "quiet_hours": "23:00-6:30",
                "channel": "wxpusher",
            }
        )
        self.assertEqual(entry["quiet"], True)
        self.assertEqual(entry["keywords"], ("考试",))
        self.assertEqual(entry["min_score"], 5)
        self.assertEqual(entry["model"], "light")
        self.assertEqual(entry["quiet_hours"], "23:00-06:30")
        self.assertNotIn("channel", entry)

    def test_normalize_entry_drops_bad_values_without_raising(self):
        entry = grouppolicy.normalize_entry({"min_score": "高", "model": "超大", "quiet": "也许"})
        self.assertEqual(entry, {})


class ParsePoliciesTest(unittest.TestCase):
    def test_bad_json_and_wrong_shape_return_empty(self):
        for raw in ("{not json", "[1, 2]", "", "3", None):
            self.assertEqual(grouppolicy.parse_policies(raw), {})

    def test_entries_are_normalized_and_unknown_ones_dropped(self):
        policies = grouppolicy.parse_policies(
            json.dumps(
                {
                    "g1": {"quiet": True, "junk": 1},
                    "g2": {"min_score": 4},
                    "g3": "没写对象",
                    "g4": {"junk": 2},
                }
            )
        )
        self.assertEqual(set(policies), {"g1", "g2"})
        self.assertEqual(policies["g1"], {"quiet": True})
        self.assertEqual(policies["g2"], {"min_score": 4})

    def test_a_mapping_is_accepted_directly(self):
        self.assertEqual(
            grouppolicy.parse_policies({"g1": {"quiet": True}}),
            {"g1": {"quiet": True}},
        )


class ClockAndKeywordTest(unittest.TestCase):
    def test_in_quiet_hours_handles_cross_midnight(self):
        policy = grouppolicy.GroupPolicy(quiet_hours="22:00-07:00")
        self.assertTrue(grouppolicy.in_quiet_hours(policy, dt.datetime(2026, 10, 9, 23, 0)))
        self.assertTrue(grouppolicy.in_quiet_hours(policy, dt.datetime(2026, 10, 9, 6, 59)))
        self.assertFalse(grouppolicy.in_quiet_hours(policy, dt.datetime(2026, 10, 9, 7, 0)))
        self.assertFalse(grouppolicy.in_quiet_hours(policy, dt.datetime(2026, 10, 9, 12, 0)))

    def test_in_quiet_hours_handles_same_day_window(self):
        policy = grouppolicy.GroupPolicy(quiet_hours="09:00-18:00")
        self.assertTrue(grouppolicy.in_quiet_hours(policy, dt.datetime(2026, 10, 9, 10, 0)))
        self.assertFalse(grouppolicy.in_quiet_hours(policy, dt.datetime(2026, 10, 9, 8, 0)))
        self.assertFalse(grouppolicy.in_quiet_hours(policy, dt.datetime(2026, 10, 9, 18, 0)))

    def test_in_quiet_hours_is_false_without_a_window(self):
        self.assertFalse(
            grouppolicy.in_quiet_hours(grouppolicy.GroupPolicy(), dt.datetime(2026, 10, 9, 23, 0))
        )

    def test_keyword_hit_returns_the_first_match(self):
        policy = grouppolicy.GroupPolicy(keywords=("考试", "选课"))
        self.assertEqual(grouppolicy.keyword_hit(policy, "关于考试的通知"), "考试")
        self.assertEqual(grouppolicy.keyword_hit(policy, "普通闲聊"), "")
        self.assertEqual(grouppolicy.keyword_hit(grouppolicy.GroupPolicy(), "考试"), "")

    def test_describe_mentions_every_configured_switch(self):
        policy = grouppolicy.GroupPolicy(
            quiet=True,
            min_score=5,
            keywords=("考试",),
            model=grouppolicy.MODEL_LIGHT,
            quiet_hours="23:00-06:30",
        )
        text = policy.describe()
        self.assertIn("安静群=是", text)
        self.assertIn("最低分=5", text)
        self.assertIn("考试", text)
        self.assertIn("轻量模型", text)
        self.assertIn("免打扰=23:00-06:30", text)


class ResolveTest(unittest.TestCase):
    def test_unconfigured_group_inherits_global_defaults(self):
        settings = policy_settings(group_policies={"g1": {"min_score": 5}})
        base = settings.group_policy("g2")
        self.assertEqual(base.min_score, 3)
        self.assertEqual(base.model, grouppolicy.MODEL_DEFAULT)
        self.assertEqual(base.overrides, ())
        self.assertFalse(base.overridden)

    def test_configured_group_records_its_overrides(self):
        settings = policy_settings(group_policies={"g1": {"quiet": True, "min_score": 5}})
        policy = settings.group_policy("g1")
        self.assertEqual(policy.min_score, 5)
        self.assertTrue(policy.quiet)
        self.assertEqual(set(policy.overrides), {"quiet", "min_score"})
        self.assertTrue(policy.overridden)

    def test_group_id_wins_over_group_name(self):
        settings = policy_settings(
            group_policies={"g1": {"min_score": 5}, "学院通知群": {"min_score": 9}}
        )
        policy = settings.group_policy("g1", "学院通知群")
        self.assertEqual(policy.matched, "g1")
        self.assertEqual(policy.min_score, 5)

    def test_group_name_is_used_when_the_id_has_no_policy(self):
        settings = policy_settings(group_policies={"学院通知群": {"min_score": 9}})
        policy = settings.group_policy("g9", "学院通知群")
        self.assertEqual(policy.matched, "学院通知群")
        self.assertEqual(policy.min_score, 9)

    def test_legacy_quiet_groups_still_apply(self):
        settings = policy_settings(quiet_groups=("g1",), group_aliases={"g1": "学院通知群"})
        self.assertTrue(settings.is_quiet_group("g1"))
        self.assertFalse(settings.is_quiet_group("g2"))
        self.assertTrue(settings.is_quiet_group_name("学院通知群"))

    def test_explicit_policy_can_turn_the_legacy_flag_off(self):
        settings = policy_settings(
            quiet_groups=("g1",),
            group_aliases={"g1": "学院通知群"},
            group_policies={"g1": {"quiet": False}},
        )
        self.assertFalse(settings.is_quiet_group("g1"))
        self.assertTrue(settings.is_quiet_group_name("学院通知群"))


class QuietGroupItemTest(unittest.TestCase):
    def test_quiet_group_is_quiet(self):
        settings = policy_settings(quiet_groups=("g1",))
        self.assertTrue(summarizer.is_quiet_group_item(item(), settings))

    def test_group_keyword_turns_a_quiet_group_into_a_notification(self):
        settings = policy_settings(
            quiet_groups=("g1",), group_policies={"g1": {"keywords": ["考试"]}}
        )
        self.assertTrue(summarizer.is_quiet_group_item(item("大家晚上好"), settings))
        self.assertFalse(summarizer.is_quiet_group_item(item("关于考试安排的通知"), settings))

    def test_quiet_hours_follow_the_message_timestamp(self):
        settings = policy_settings(group_policies={"g1": {"quiet_hours": "22:00-07:00"}})
        late = dt.datetime(2026, 10, 9, 23, 30)
        noon = dt.datetime(2026, 10, 9, 12, 0)
        self.assertTrue(summarizer.is_quiet_group_item(item("通知", moment=late), settings))
        self.assertFalse(summarizer.is_quiet_group_item(item("通知", moment=noon), settings))

    def test_keyword_still_wins_inside_quiet_hours(self):
        settings = policy_settings(
            group_policies={"g1": {"quiet_hours": "22:00-07:00", "keywords": ["考试"]}}
        )
        late = dt.datetime(2026, 10, 9, 23, 30)
        self.assertFalse(summarizer.is_quiet_group_item(item("考试通知", moment=late), settings))


class FocusReasonTest(unittest.TestCase):
    def test_group_keyword_short_circuits_to_a_notification(self):
        settings = policy_settings(
            quiet_groups=("g1",), group_policies={"g1": {"keywords": ["考试"]}}
        )
        self.assertEqual(summarizer.focus_reason(item("关于考试安排的通知"), settings), "")

    def test_per_group_min_score_is_named_in_the_reason(self):
        settings = policy_settings(min_score=3, group_policies={"g1": {"min_score": 5}})
        self.assertEqual(
            summarizer.focus_reason(item("普通闲聊", score=4), settings),
            "分值 4 < 本群阈值 5",
        )

    def test_global_threshold_wording_is_unchanged(self):
        settings = policy_settings(min_score=3)
        self.assertEqual(
            summarizer.focus_reason(item("普通闲聊", score=2), settings),
            "分值 2 < 阈值 3",
        )


class PinnedRouteTest(unittest.TestCase):
    def test_single_group_can_be_pinned_to_the_rule_tier(self):
        decision = decide_route(
            [high_item()], route_settings(group_policies={"g1": {"model": "rule"}})
        )
        self.assertEqual(decision.tier, llmstats.ROUTE_RULE)
        self.assertEqual(decision.model, "")
        self.assertIn("本地规则", decision.reason)

    def test_single_group_can_be_pinned_to_the_light_model(self):
        decision = decide_route(
            [high_item()], route_settings(group_policies={"g1": {"model": "light"}})
        )
        self.assertEqual(decision.tier, llmstats.ROUTE_LIGHT)
        self.assertEqual(decision.model, "qwen-turbo")
        self.assertIn("轻量模型", decision.reason)

    def test_single_group_can_be_pinned_to_the_strong_model(self):
        decision = decide_route(
            [high_item()], route_settings(group_policies={"g1": {"model": "strong"}})
        )
        self.assertEqual(decision.tier, llmstats.ROUTE_STRONG)
        self.assertEqual(decision.model, "qwen-plus")

    def test_light_pin_falls_back_when_no_light_model_is_configured(self):
        settings = route_settings(group_policies={"g1": {"model": "light"}}, llm_model_light="")
        decision = decide_route([high_item()], settings)
        self.assertNotEqual(decision.tier, llmstats.ROUTE_LIGHT)
        self.assertIn("未配置轻量模型", decision.reason)

    def test_a_mixed_batch_ignores_the_pin(self):
        items = [high_item(), {**high_item(), "group_id": "g9", "group_name": "别的群"}]
        decision = decide_route(items, route_settings(group_policies={"g1": {"model": "rule"}}))
        self.assertNotIn("本群策略", decision.reason)

    def test_a_policy_keyed_by_group_name_still_pins(self):
        pinned = {**high_item(), "group_id": ""}
        decision = decide_route(
            [pinned], route_settings(group_policies={"学院通知群": {"model": "rule"}})
        )
        self.assertEqual(decision.tier, llmstats.ROUTE_RULE)


class GroupsCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.env_file = self.data_dir / "test.env"
        self._saved = {key: os.environ.get(key) for key in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        self.addCleanup(self._restore_env)
        self.addCleanup(self._close_log_handlers)

    def _write_env(self, lines: list[str]) -> None:
        text = [
            f"QQ_DIGEST_DATA_DIR={self.data_dir.as_posix()}",
            f"QQ_DIGEST_LOG_DIR={(self.data_dir / 'logs').as_posix()}",
            *lines,
        ]
        self.env_file.write_text("\n".join(text) + "\n", encoding="utf-8")

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
            code = main.main(["--env", str(self.env_file), "groups", *extra])
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_lists_each_group_with_its_effective_switches(self):
        self._write_env(
            [
                "QQ_DIGEST_GROUPS=123456,学院通知群",
                "QQ_DIGEST_GROUP_ALIASES=123456:学院通知群",
                'QQ_DIGEST_GROUP_POLICIES={"123456": {"quiet": true, "min_score": 5,'
                ' "keywords": ["考试"]}}',
            ]
        )
        text = self._run()
        self.assertIn("群级策略 · 2 个群", text)
        self.assertIn("本群覆盖：quiet、min_score、keywords", text)
        self.assertIn("全部继承全局", text)
        self.assertIn("考试", text)

    def test_json_output_is_machine_readable(self):
        self._write_env(
            [
                "QQ_DIGEST_GROUPS=123456",
                "QQ_DIGEST_GROUP_ALIASES=123456:学院通知群",
                'QQ_DIGEST_GROUP_POLICIES={"123456": {"quiet": true}}',
            ]
        )
        payload = json.loads(self._run("--json"))
        self.assertEqual(payload["groups"][0]["group_id"], "123456")
        self.assertIs(payload["groups"][0]["quiet"], True)
        self.assertEqual(payload["unused_policies"], [])
        self.assertIn("model", payload["fields"])

    def test_unused_policy_keys_are_reported(self):
        self._write_env(
            [
                "QQ_DIGEST_GROUPS=123456",
                'QQ_DIGEST_GROUP_POLICIES={"999999": {"quiet": true}}',
            ]
        )
        text = self._run()
        self.assertIn("999999", text)
        self.assertIn("没有匹配到白名单群", text)

    def test_without_any_group_it_says_so(self):
        self._write_env([])
        self.assertIn("还没有配置任何群", self._run())


class DoctorGroupsTest(unittest.TestCase):
    def test_hint_points_at_the_groups_command(self):
        settings = Settings(group_whitelist=("123456", "654321"))
        check = check_groups(DoctorContext(settings=settings))
        self.assertEqual(check.status, OK)
        self.assertIn("main.py groups", check.hint)

    def test_policy_count_is_mentioned(self):
        settings = Settings(
            group_whitelist=("123456", "654321"),
            group_policies={"123456": {"quiet": True}},
        )
        check = check_groups(DoctorContext(settings=settings))
        self.assertIn("配了群策略", check.detail)


if __name__ == "__main__":
    unittest.main()
