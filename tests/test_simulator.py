"""假群聊生成器（Roadmap A20）回归测试。

要守住两件事：

1. **同一份参数必得同一份数据**——否则拿它做回归对比毫无意义；
2. **回放走的是真实链路**——入库、判定、摘要、投递、决策日志一个环节都不许绕开，
   同时保证它是离线的：没有真实通道、没有大模型、不读 `.env`、不碰 `data/`。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest import decisions, simulator  # noqa: E402


class GenerateTest(unittest.TestCase):
    def test_same_seed_gives_identical_data(self) -> None:
        self.assertEqual(simulator.generate(60, seed=11), simulator.generate(60, seed=11))

    def test_different_seed_gives_different_data(self) -> None:
        self.assertNotEqual(simulator.generate(60, seed=11), simulator.generate(60, seed=12))

    def test_records_look_like_onebot_events(self) -> None:
        records = simulator.generate(80, seed=5)
        known_groups = {item["group_id"] for item in simulator.GROUPS}
        for record in records:
            with self.subTest(msg_id=record["msg_id"]):
                self.assertTrue(record["msg_id"])
                self.assertIn(record["group_id"], known_groups)
                self.assertTrue(record["content"].strip())
                self.assertIn(record["event"], {"GROUP_MESSAGE_CREATE", "GROUP_FILE_UPLOAD"})
                self.assertIn("T", record["received_at"])
                self.assertEqual(record["ts"], record["received_at"])
        self.assertEqual(len({item["msg_id"] for item in records}), len(records))
        stamps = [item["received_at"] for item in records]
        self.assertEqual(stamps, sorted(stamps))

    def test_mix_contains_chatter_and_real_notices(self) -> None:
        """数据必须两类都有，否则回放出来的漏斗是假的。"""
        text = " ".join(item["content"] for item in simulator.generate(400, seed=3))
        self.assertIn("哈哈哈哈", text)
        self.assertIn("【学院通知】", text)
        self.assertIn("【作业】", text)
        self.assertIn("【兼职】", text)

    def test_count_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            simulator.generate(0)


class FixtureTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            records = simulator.generate(25, seed=4)
            self.assertEqual(simulator.write_fixture(path, records), 25)
            self.assertEqual(simulator.read_fixture(path), records)


class SimulatorSettingsTest(unittest.TestCase):
    """离线是硬承诺：配置里不许出现任何真实通道或大模型凭证。"""

    def test_settings_never_reach_real_channels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = simulator.simulator_settings(tmp)
        for field in (
            "wxpusher_app_token",
            "wxpusher_uids",
            "serverchan_keys",
            "pushplus_tokens",
            "webhook_urls",
            "dashscope_api_key",
        ):
            with self.subTest(field=field):
                self.assertFalse(getattr(settings, field))
        self.assertFalse(settings.llm_enabled)
        self.assertFalse(settings.onebot_enabled)
        self.assertFalse(settings.official_bot_enabled)
        self.assertFalse(settings.catchup_enabled)
        self.assertTrue(settings.quiet_groups)

    def test_channel_is_in_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _service, channel = simulator.build_service(tmp)
        self.assertEqual(channel.name, "simulator")
        self.assertEqual(channel.sent, [])


class ReplayTest(unittest.TestCase):
    def test_replay_walks_the_real_pipeline(self) -> None:
        records = simulator.generate(180, seed=20261008, span_hours=6)
        with tempfile.TemporaryDirectory() as tmp:
            report = simulator.replay(records, data_dir=tmp, daily_budget=0, step_minutes=10)
        self.assertEqual(report.messages, 180)
        self.assertGreaterEqual(report.pushed, 1)
        self.assertGreaterEqual(report.push_items, 1)
        self.assertGreater(report.decisions.get(decisions.FILTERED, 0), 0)
        self.assertGreater(report.decisions.get(decisions.PUSHED, 0), 0)
        self.assertTrue(report.first_title)
        self.assertIn("QQ", report.first_body)
        text = " ".join(report.summary_lines())
        self.assertIn("未命中", text)
        self.assertIn("命中率", text)

    def test_replay_is_reproducible(self) -> None:
        records = simulator.generate(150, seed=77, span_hours=5)
        runs = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as tmp:
                report = simulator.replay(records, data_dir=tmp, daily_budget=0, step_minutes=10)
            runs.append((report.pushed, report.push_items, report.decisions, report.first_body))
        self.assertEqual(runs[0], runs[1])

    def test_exhausted_budget_or_quiet_hours_leave_a_deferred_trail(self) -> None:
        """额度用尽 / 夜间静默的消息不会凭空消失，而是留下「延后未决」。"""
        records = simulator.generate(200, seed=42, span_hours=6)
        with tempfile.TemporaryDirectory() as tmp:
            report = simulator.replay(
                records, data_dir=tmp, daily_budget=1, step_minutes=10
            )
        self.assertGreater(report.deferred, 0)
        self.assertIn("延后未决", " ".join(report.summary_lines()))


if __name__ == "__main__":
    unittest.main()
