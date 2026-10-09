"""脱敏评测集（Roadmap A21）回归测试。

要守住三件事：

1. 评测集的 ground truth 是模拟器生成时写进去的，能随 fixture 往返；
2. 指标可复现：同 seed 跑两遍必得同一组数字；
3. 基线不许倒退：召回 / 误报 / 待办 / 截止时间这些硬指标有下限。

全程离线：临时目录 + 内存假通道，不碰 ``data/``、不读 ``.env``、不调大模型。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402
from qq_live_digest import benchmark, simulator  # noqa: E402


class ExpectationTest(unittest.TestCase):
    def test_every_record_carries_intent_labels(self) -> None:
        records = simulator.generate(80, seed=9)
        kinds = {item["_expect"]["kind"] for item in records}
        for record in records:
            with self.subTest(msg_id=record["msg_id"]):
                expected = record["_expect"]
                self.assertIn(expected["kind"], simulator.KIND_EXPECTATIONS)
                self.assertIsInstance(expected["todo"], bool)
                self.assertIsInstance(expected["deadline"], bool)
                self.assertTrue(expected["push"] is None or isinstance(expected["push"], bool))
        self.assertGreaterEqual(len(kinds), 2)
        self.assertIn("chatter", kinds)

    def test_fixture_round_trip_keeps_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bench.jsonl"
            records = simulator.generate(25, seed=4)
            simulator.write_fixture(path, records)
            self.assertEqual(simulator.read_fixture(path), records)

    def test_expectation_matches_template_mapping(self) -> None:
        by_kind = {item.kind: item for item in simulator.TEMPLATES}
        self.assertEqual(set(by_kind), set(simulator.KIND_EXPECTATIONS))
        self.assertIs(simulator.expectation(by_kind["chatter"])["push"], False)
        self.assertIs(simulator.expectation(by_kind["notice"])["push"], True)
        self.assertTrue(simulator.expectation(by_kind["notice"])["deadline"])
        self.assertFalse(simulator.expectation(by_kind["chatter"])["deadline"])
        self.assertIs(simulator.expectation(by_kind["file"])["push"], None)


class EvaluateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.records = simulator.generate(150, seed=5)

    def _evaluate(self) -> benchmark.BenchmarkReport:
        with tempfile.TemporaryDirectory() as tmp:
            return benchmark.evaluate(self.records, data_dir=tmp, seed=5)

    def test_missing_labels_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                benchmark.evaluate([{"msg_id": "x", "content": "y"}], data_dir=tmp)

    def test_report_is_self_consistent(self) -> None:
        report = self._evaluate()
        self.assertEqual(report.messages, 150)
        self.assertEqual(sum(item["total"] for item in report.per_kind.values()), 150)
        self.assertGreater(report.positives, 0)
        self.assertGreater(report.negatives, 0)
        self.assertLessEqual(report.detected_positives, report.positives)
        self.assertLessEqual(report.delivered_positives, report.detected_positives)
        self.assertLessEqual(report.false_positives, report.negatives)
        self.assertLessEqual(report.todo_created, report.todo_expected)
        self.assertLessEqual(report.deadline_parsed, report.deadline_expected)
        self.assertLessEqual(report.dedupe_hit, report.dedupe_expected)
        self.assertIn("filtered", report.funnel)

    def test_baseline_quality_floor(self) -> None:
        report = self._evaluate()
        self.assertGreaterEqual(report.recall, 0.9)
        self.assertLessEqual(report.false_positive_rate, 0.1)
        self.assertGreaterEqual(report.todo_rate, 0.9)
        self.assertGreaterEqual(report.deadline_rate, 0.9)

    def test_reproducible_across_runs(self) -> None:
        self.assertEqual(self._evaluate().as_dict(), self._evaluate().as_dict())

    def test_json_payload_is_machine_readable(self) -> None:
        payload = self._evaluate().as_dict()
        json.dumps(payload)
        for key in ("recall", "false_positive_rate", "todo_rate", "deadline_rate", "per_kind"):
            self.assertIn(key, payload)

    def test_run_defaults_to_a_temp_dir(self) -> None:
        report = benchmark.run(count=60, seed=11)
        self.assertEqual(report.messages, 60)

    def test_fixture_input_scores_the_same(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bench.jsonl"
            records = simulator.generate(40, seed=8)
            simulator.write_fixture(path, records)
            loaded = simulator.read_fixture(path)
            with tempfile.TemporaryDirectory() as work:
                report = benchmark.evaluate(loaded, data_dir=work, seed=8)
        self.assertEqual(report.messages, 40)


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env_file = Path(self._tmp.name) / ".env"
        self.env_file.write_text("", encoding="utf-8")

    def _run(self, *extra: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main.main(["--env", str(self.env_file), "benchmark", *extra])
        return code, buffer.getvalue()

    def test_text_mode_prints_headline_metrics(self) -> None:
        code, text = self._run("--count", "80", "--seed", "3")
        self.assertEqual(code, 0)
        for token in ("评测集", "召回", "误报", "待办", "截止时间", "跨群去重", "延迟", "成本"):
            self.assertIn(token, text)

    def test_json_mode_is_machine_readable(self) -> None:
        code, text = self._run("--count", "80", "--seed", "3", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(text[text.index("{") :])
        self.assertEqual(payload["messages"], 80)
        self.assertIn("recall", payload)
        self.assertTrue(payload["per_kind"])

    def test_fail_under_gate_exits_nonzero(self) -> None:
        code, text = self._run("--count", "80", "--seed", "3", "--fail-under", "1.5")
        self.assertEqual(code, 1)
        self.assertIn("低于门槛", text)

    def test_alias_bench_works(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main.main(["--env", str(self.env_file), "bench", "--count", "50", "--seed", "2", "--json"])
        self.assertEqual(code, 0)
        self.assertIn('"messages": 50', buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
