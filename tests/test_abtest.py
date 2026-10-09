"""Prompt / 模型 A/B（Roadmap A22）回归测试。

守住：预设与自定义配置解析、benchmark 参数与 LLM 旋钮的拆分、质量分对误报的惩罚、
同一评测集上跑两套配置并选出胜者，以及 prompt 档位确实改了提示词。
"""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import qq_digest  # noqa: E402
from qq_live_digest import abtest  # noqa: E402
from qq_live_digest.config import Settings  # noqa: E402


class ResolveTest(unittest.TestCase):
    def test_presets_have_labels_and_options(self) -> None:
        self.assertIn("default", abtest.PRESETS)
        for name, preset in abtest.PRESETS.items():
            self.assertTrue(preset["label"], name)
            self.assertIsInstance(preset["options"], dict)

    def test_resolve_preset_name(self) -> None:
        cfg = abtest.resolve("strict")
        self.assertEqual(cfg["name"], "strict")
        self.assertEqual(cfg["options"], {"min_score": 4})

    def test_resolve_custom_dict(self) -> None:
        cfg = abtest.resolve({"label": "我的配置", "min_score": 5, "prompt_profile": "terse"})
        self.assertEqual(cfg["label"], "我的配置")
        self.assertEqual(cfg["options"], {"min_score": 5, "prompt_profile": "terse"})

    def test_unknown_preset_raises(self) -> None:
        with self.assertRaises(KeyError):
            abtest.resolve("nope")

    def test_parse_spec_json_or_name(self) -> None:
        self.assertEqual(abtest.parse_spec("loose"), "loose")
        self.assertEqual(abtest.parse_spec('{"min_score": 3}'), {"min_score": 3})
        with self.assertRaises(ValueError):
            abtest.parse_spec("{not json")


class OptionsTest(unittest.TestCase):
    def test_split_benchmark_and_llm_knobs(self) -> None:
        bench, llm = abtest.split_options(
            {"min_score": 4, "window_minutes": 60, "prompt_profile": "terse", "model": "qwen-light"}
        )
        self.assertEqual(bench, {"min_score": 4, "window_minutes": 60})
        self.assertEqual(llm, {"prompt_profile": "terse", "model": "qwen-light"})


class QualityTest(unittest.TestCase):
    def test_false_positive_is_penalised(self) -> None:
        clean = {"recall": 1.0, "todo_rate": 1.0, "deadline_rate": 1.0, "dedupe_rate": 1.0, "false_positive_rate": 0.0}
        noisy = dict(clean, false_positive_rate=0.5)
        self.assertGreater(abtest.quality(clean), abtest.quality(noisy))
        self.assertAlmostEqual(abtest.quality(clean), 0.8, places=4)


class CompareTest(unittest.TestCase):
    def test_compare_two_presets_on_one_dataset(self) -> None:
        report = abtest.compare(["default", "strict"], count=60, seed=20261008)
        self.assertTrue(report["ok"])
        self.assertEqual(report["dataset"]["messages"], 60)
        self.assertEqual([entry["name"] for entry in report["configs"]], ["default", "strict"])
        self.assertIn(report["winner"], {"default", "strict"})
        self.assertIn("cost_yuan", report["delta"])
        for entry in report["configs"]:
            self.assertIn("recall", entry["metrics"])
            self.assertIn("quality", entry)

    def test_empty_specs_raise(self) -> None:
        with self.assertRaises(ValueError):
            abtest.compare([])

    def test_render_reports_winner(self) -> None:
        text = abtest.render(abtest.compare(["default", "loose"], count=60, seed=20261008))
        self.assertIn("结论：", text)
        self.assertIn("默认", text)
        self.assertIn("宽松", text)


class PromptProfileTest(unittest.TestCase):
    def _items(self) -> list[dict]:
        message = qq_digest.Message(dt.datetime(2026, 9, 30, 12, 0), "辅导员", "明天下午三点开组会")
        return [{"message": message, "category": "action", "deadline": None}]

    def test_default_prompt_has_no_style_note(self) -> None:
        messages = qq_digest.build_refine_messages(self._items())
        self.assertNotIn("额外要求", messages[1]["content"])

    def test_terse_prompt_adds_note(self) -> None:
        messages = qq_digest.build_refine_messages(self._items(), profile="terse")
        self.assertIn(qq_digest.PROMPT_PROFILES["terse"], messages[1]["content"])

    def test_profile_from_env(self) -> None:
        settings = Settings.from_env(env={"QQ_DIGEST_PROMPT_PROFILE": "detailed"})
        self.assertEqual(settings.prompt_profile, "detailed")
        self.assertEqual(Settings.from_env(env={}).prompt_profile, "")


if __name__ == "__main__":
    unittest.main()
