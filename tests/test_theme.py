"""界面配色单一来源（theme.py）的回归测试。

这一层要防的是一类很隐蔽的漂移：主题 token 改了、但页面或演示片还留着旧值。
所以既测 theme 自身（两套 key/顺序一致、值合法），也测「页面与演示片确实用的是它」。
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest import demo, theme, webapp  # noqa: E402


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(value: int) -> float:
        v = value / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class ThemeShapeTest(unittest.TestCase):
    def test_two_schemes_share_the_same_keys_in_the_same_order(self) -> None:
        dark = [name for name, _ in theme.pairs("dark")]
        light = [name for name, _ in theme.pairs("light")]
        self.assertEqual(dark, light)
        self.assertEqual(len(dark), len(set(dark)))
        self.assertEqual(dark[0], "--bg")
        self.assertEqual(dark[-1], "color-scheme")

    def test_values_cannot_break_out_of_the_declaration_block(self) -> None:
        for scheme in ("dark", "light"):
            for name, value in theme.pairs(scheme):
                self.assertTrue(name == "color-scheme" or name.startswith("--"), name)
                self.assertTrue(value)
                for bad in (";", "{", "}", "<", "\\", '"'):
                    self.assertNotIn(bad, value, f"{scheme} {name}")

    def test_unknown_scheme_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            theme.tokens("midnight")
        with self.assertRaises(ValueError):
            theme.css_block("midnight")

    def test_css_block_is_one_declaration_per_line(self) -> None:
        block = theme.css_block("dark")
        self.assertEqual(len(block.splitlines()), len(theme.DARK))
        self.assertIn("  --bg:#0E1117;", block)
        self.assertTrue(block.endswith("  color-scheme:dark;"))
        parsed = dict(re.findall(r"(--[\w-]+|color-scheme):([^;]+);", block))
        self.assertEqual(parsed, theme.tokens("dark"))

    def test_hex_rgb_only_accepts_solid_hex_tokens(self) -> None:
        self.assertEqual(theme.hex_rgb("--bg"), (14, 17, 23))
        self.assertEqual(theme.hex_rgb("--text", "light"), (25, 29, 36))
        with self.assertRaises(ValueError):
            theme.hex_rgb("--shadow")  # rgba(...) 不是十六进制
        with self.assertRaises(KeyError):
            theme.hex_rgb("--nope")


class PageUsesTheTokensTest(unittest.TestCase):
    def test_page_has_both_blocks_and_no_leftover_placeholder(self) -> None:
        self.assertNotIn("__THEME", webapp.PAGE_HTML)
        for scheme in ("dark", "light"):
            for name, value in theme.pairs(scheme):
                self.assertIn(f"{name}:{value};", webapp.PAGE_HTML, f"{scheme} {name}")

    def test_light_block_only_overrides_names_the_dark_block_defines(self) -> None:
        dark = webapp.PAGE_HTML.split(":root{", 1)[1].split("}", 1)[0]
        light = webapp.PAGE_HTML.split('[data-theme="light"]{', 1)[1].split("}", 1)[0]
        dark_names = set(re.findall(r"(--[\w-]+|color-scheme):", dark))
        light_names = set(re.findall(r"(--[\w-]+|color-scheme):", light))
        self.assertEqual(light_names, dark_names)

    def test_icon_background_matches_the_manifest_theme_color(self) -> None:
        theme_color = json.loads(webapp.MANIFEST_JSON)["theme_color"]
        self.assertEqual(theme_color, theme.tokens("dark")["--bg"])
        self.assertIn(f'fill="{theme_color}"', webapp.ICON_SVG)


class DemoPaletteUsesTheTokensTest(unittest.TestCase):
    def test_demo_palette_is_the_dark_token_set(self) -> None:
        self.assertEqual(demo.BG, theme.hex_rgb("--bg"))
        self.assertEqual(demo.PANEL, theme.hex_rgb("--card"))
        self.assertEqual(demo.BORDER, theme.hex_rgb("--line"))
        self.assertEqual(demo.FG, theme.hex_rgb("--text"))
        self.assertEqual(demo.MUTED, theme.hex_rgb("--muted"))
        self.assertEqual(demo.ACCENT, theme.hex_rgb("--accent-soft"))
        self.assertEqual(demo.WARN, theme.hex_rgb("--action"))
        self.assertEqual(demo.INK, theme.hex_rgb("--bg"))

    def test_funnel_bar_labels_stay_readable_on_their_own_bar(self) -> None:
        # 漏斗四行的数字都画在填充条上（INK），条一满就该看得清；这是换配色时最容易踩的坑
        for fill in (demo.ACCENT, demo.MUTED, demo.GOOD, demo.WARN):
            self.assertGreaterEqual(_contrast(demo.INK, fill), 4.5, fill)


if __name__ == "__main__":
    unittest.main()
