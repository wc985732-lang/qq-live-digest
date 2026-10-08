"""可选依赖降级测试（2026-10-08 安全审计回归项）。

审计环境未安装 qq-botpy，133 项测试里有 1 项直接报错：`qq_live_digest/bot.py` 在 import 阶段
`raise SystemExit`，而 `service.py` 又是在模块级 `from .bot import BotRunner`——于是
"少装一个可选依赖"被放大成"整个程序 / 整个测试套件退出"。

本文件把这条边界固化下来：**没有 qq-botpy 时，其余能力必须照常可用**。
因为 `qq_live_digest.service` 会在本进程里被其它用例导入，这里统一用**子进程 + 导入拦截器**
模拟"干净环境里没装 qq-botpy"，避免污染本进程的 sys.modules。
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 子进程前置代码：把 botpy / botpy.* 的导入直接判为失败。
BLOCK_BOTPY = """
import importlib.abc
import sys


class _BlockBotpy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "botpy" or fullname.startswith("botpy."):
            raise ModuleNotFoundError("No module named %r (blocked for test)" % fullname)
        return None


sys.meta_path.insert(0, _BlockBotpy())
"""


def run_python(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


def run_without_botpy(body: str) -> subprocess.CompletedProcess:
    return run_python(BLOCK_BOTPY + body)


class MissingBotpyTest(unittest.TestCase):
    """缺 qq-botpy 时，只允许官方机器人功能降级，不允许整个程序倒下。"""

    def test_service_and_bot_import_without_botpy(self) -> None:
        proc = run_without_botpy(
            "import qq_live_digest.bot as bot\n"
            "import qq_live_digest.service as service\n"
            "assert service.BotRunner is bot.BotRunner\n"
            "assert callable(bot.install_full_group_message_parser)\n"
            "assert bot.botpy_available() is False\n"
            "print('imports-ok')\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("imports-ok", proc.stdout)

    def test_unrelated_modules_import_without_botpy(self) -> None:
        proc = run_without_botpy(
            "import qq_live_digest.doctor\n"
            "import qq_live_digest.push\n"
            "import qq_live_digest.summarizer\n"
            "import qq_live_digest.webapp\n"
            "import main\n"
            "print('imports-ok')\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("imports-ok", proc.stdout)

    def test_client_access_raises_actionable_error(self) -> None:
        proc = run_without_botpy(
            "import qq_live_digest.bot as bot\n"
            "try:\n"
            "    bot.LiveBotClient\n"
            "except bot.BotpyNotInstalled as error:\n"
            "    assert 'qq-botpy' in str(error)\n"
            "    print('raised-as-designed')\n"
            "else:\n"
            "    raise AssertionError('LiveBotClient must not resolve without botpy')\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("raised-as-designed", proc.stdout)

    def test_botrunner_start_fails_softly_without_botpy(self) -> None:
        proc = run_without_botpy(
            "import tempfile\n"
            "from pathlib import Path\n"
            "from qq_live_digest.bot import MISSING_BOTPY_HINT, BotRunner\n"
            "from qq_live_digest.config import Settings\n"
            "settings = Settings(appid='12345', secret='secret', data_dir=Path(tempfile.mkdtemp()))\n"
            "runner = BotRunner(settings, lambda record: None)\n"
            "assert runner.start() is False\n"
            "assert runner.last_error == MISSING_BOTPY_HINT\n"
            "assert runner.is_alive is False\n"
            "print('soft-fail')\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("soft-fail", proc.stdout)


class BotpyInstalledTest(unittest.TestCase):
    """装了 qq-botpy 时，延迟导入必须给出真正的类，而不是缩水版。"""

    def test_lazy_access_matches_real_botpy(self) -> None:
        proc = run_python(
            "import botpy\n"
            "import qq_live_digest.bot as bot\n"
            "assert bot.botpy_available() is True\n"
            "assert bot.botpy_version() != 'unknown'\n"
            "assert issubclass(bot.LiveBotClient, botpy.Client)\n"
            "assert bot.botpy.Intents is botpy.Intents\n"
            "print('lazy-ok')\n"
        )
        if proc.returncode != 0 and "No module named 'botpy'" in proc.stderr:
            self.skipTest("本机未安装 qq-botpy")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("lazy-ok", proc.stdout)
