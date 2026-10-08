"""演示短片生成器（Roadmap A27）回归测试。

这个片子的全部价值在于「数字是真的」——所以测试盯的也是这件事：片子里出现的条数
必须等于那一次真实回放的结果，而且必须带「示例数据」水印。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest import demo  # noqa: E402

try:  # 渲染需要 Pillow（它本来就在 requirements.txt 里，CI 也装了）
    from PIL import Image  # noqa: F401

    HAS_PIL = True
except Exception:  # pragma: no cover - 极简环境下跳过渲染用例
    HAS_PIL = False


class CollectTest(unittest.TestCase):
    def test_demo_uses_numbers_from_a_real_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = demo.collect(tmp, count=150, seed=99, span_hours=5, daily_budget=0)
        self.assertEqual(data.message_count, 150)
        self.assertGreater(data.filtered, 0)
        self.assertGreater(data.pushed, 0)
        self.assertGreater(data.push_items, 0)
        self.assertEqual(data.message_count, data.filtered + data.deduped + data.candidates)

    def test_collected_text_is_usable_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = demo.collect(tmp, count=150, seed=7, span_hours=5, daily_budget=0)
        self.assertTrue(data.stream)
        self.assertGreater(len(data.reasons), 1)
        self.assertEqual(len(set(data.reasons)), len(data.reasons))
        self.assertTrue(data.digest_lines)
        self.assertTrue(data.log_lines)
        self.assertTrue(data.sample_msg_id.startswith("sim-"))

    def test_banner_carries_the_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = demo.collect(tmp, count=60, seed=1, span_hours=2, daily_budget=0)
        self.assertIn(demo.WATERMARK, demo.banner_text(data))

    def test_script_length_is_in_the_promised_range(self) -> None:
        self.assertGreaterEqual(demo.total_seconds(), 20.0)
        self.assertLessEqual(demo.total_seconds(), 30.0)


@unittest.skipUnless(HAS_PIL, "需要 Pillow 才能渲染")
class RenderTest(unittest.TestCase):
    def test_render_writes_an_animated_gif(self) -> None:
        from PIL import Image

        data = demo.DemoData(message_count=10, pushed=1, push_items=2, tasks=1, filtered=5)
        with tempfile.TemporaryDirectory() as tmp:
            path = demo.render(data, Path(tmp) / "demo.gif", scale=0.3, fps=4, colors=32)
            self.assertTrue(path.exists())
            self.assertGreater(path.stat().st_size, 0)
            with Image.open(path) as image:
                self.assertEqual(image.format, "GIF")
                self.assertGreater(image.n_frames, 20)

    def test_empty_data_still_renders(self) -> None:
        """没跑过任何数据时也不该崩，只是画面朴素一点。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = demo.render(demo.DemoData(), Path(tmp) / "empty.gif", scale=0.25, fps=4)
            self.assertTrue(path.exists())
            self.assertGreater(path.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
