"""全链路自检（doctor）的离线测试。

所有外部探测都通过 DoctorContext 注入，因此本文件不发任何真实网络请求，
也不依赖 NapCat / QQ / 真实群号。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest import doctor as doctor_module  # noqa: E402
from qq_live_digest.catchup import NapCatError  # noqa: E402
from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.doctor import (  # noqa: E402
    FAIL,
    OK,
    WARN,
    Check,
    DoctorContext,
    _health_host,
    _is_loopback,
    _mask_id,
    as_dicts,
    check_access,
    check_bot_credentials,
    check_env_file,
    check_groups,
    check_llm,
    check_napcat,
    check_onebot,
    check_push,
    check_python,
    check_receiver,
    check_storage,
    check_web,
    render,
    run_checks,
    worst_status,
)
from qq_live_digest.store import Store  # noqa: E402

GROUP_A = "12345678"
GROUP_B = "87654321"
WEB_TOKEN = "web-token-abcdef"
ONEBOT_TOKEN = "onebot-token-xyz"
API_KEY = "sk-dashscope-secret"


class _FakeNapCat:
    """可选地返回 online/good，或按注入的异常失败。"""

    def __init__(self, *, error: Exception | None = None, status=None, login=None):
        self.error = error
        self._status = status if status is not None else {"data": {"online": True, "good": True}}
        self._login = login if login is not None else {"user_id": "3566526103"}

    def call(self, action: str):
        if self.error is not None:
            raise self.error
        return self._status

    def login_info(self):
        return self._login


def _settings(**overrides) -> Settings:
    base = dict(
        group_whitelist=(GROUP_A, GROUP_B),
        wxpusher_app_token="app-token",
        wxpusher_uids=["UID_x"],
        onebot_enabled=True,
        onebot_host="127.0.0.1",
        onebot_port=8765,
        onebot_token=ONEBOT_TOKEN,
        web_host="127.0.0.1",
        web_port=8766,
        web_token=WEB_TOKEN,
        dashscope_api_key=API_KEY,
    )
    base.update(overrides)
    return Settings(**base)


def _health_response(url: str, timeout: int) -> tuple[int, str]:
    if ":8765" in url:
        return 200, json.dumps({"ok": True, "service": "qq-live-digest"})
    return 200, json.dumps({"ok": True, "service": "qq-tasks"})


class DoctorFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "doctor.sqlite3")

    def context(self, settings: Settings | None = None, **overrides) -> DoctorContext:
        if settings is None:
            env = Path(self.tmp.name) / ".env"
            env.write_text("", encoding="utf-8")
            settings = _settings(env_file=env)
        kwargs = dict(
            settings=settings,
            http_get=_health_response,
            napcat_factory=lambda *args, **kw: _FakeNapCat(),
            open_store=lambda: self.store,
            python_version=(3, 12, 4),
        )
        kwargs.update(overrides)
        return DoctorContext(**kwargs)


class RedactionTest(DoctorFixture):
    """doctor 输出经常被贴进 Issue，必须脱敏。"""

    def test_render_does_not_leak_secrets_or_ids(self) -> None:
        settings = _settings(env_file=Path(self.tmp.name) / "secret-dir" / ".env")
        output = render(run_checks(self.context(settings)))
        for secret in (ONEBOT_TOKEN, WEB_TOKEN, API_KEY):
            self.assertNotIn(secret, output, "输出泄露了凭证")
        for group in (GROUP_A, GROUP_B):
            self.assertNotIn(group, output, "输出泄露了完整群号")
        self.assertNotIn("secret-dir", output, "输出泄露了本机绝对路径")
        self.assertIn("已脱敏", output)

    def test_json_output_does_not_leak_secrets(self) -> None:
        payload = json.dumps(as_dicts(run_checks(self.context())), ensure_ascii=False)
        for secret in (ONEBOT_TOKEN, WEB_TOKEN, API_KEY):
            self.assertNotIn(secret, payload)
        self.assertNotIn(GROUP_A, payload)

    def test_mask_id_keeps_short_values_opaque(self) -> None:
        self.assertEqual(_mask_id(""), "")
        self.assertEqual(_mask_id("123"), "***")
        self.assertEqual(_mask_id("12345678"), "12***78")

    def test_web_token_is_never_printed_even_when_set(self) -> None:
        settings = _settings(web_host="0.0.0.0")
        checks = run_checks(self.context(settings))
        detail = " ".join(item.detail for item in checks)
        self.assertNotIn(WEB_TOKEN, detail)


class HealthyEnvironmentTest(DoctorFixture):
    def test_healthy_environment_has_no_failures(self) -> None:
        checks = run_checks(self.context())
        self.assertEqual([item.name for item in checks if item.status == FAIL], [])
        self.assertEqual(worst_status(checks), OK)

    def test_every_check_has_a_detail(self) -> None:
        for item in run_checks(self.context()):
            self.assertTrue(item.detail, f"{item.name} 没有 detail")

    def test_every_non_ok_check_gives_a_hint(self) -> None:
        """可执行性是这次改造的核心：有问题必须告诉用户下一步做什么。"""
        broken = _settings(group_whitelist=(), wxpusher_app_token="", wxpusher_uids=[])
        checks = run_checks(
            self.context(
                broken,
                http_get=lambda url, timeout: (_ for _ in ()).throw(OSError("refused")),
                napcat_factory=lambda *args, **kw: _FakeNapCat(error=NapCatError("get_status 请求失败: refused")),
            )
        )
        self.assertTrue(any(item.status == FAIL for item in checks))
        for item in checks:
            if item.status != OK:
                self.assertTrue(item.hint, f"{item.name} 状态 {item.status} 但没有给建议")


class UnconfiguredEnvironmentTest(DoctorFixture):
    """刚 clone 下来、什么都没有时，doctor 必须能跑完并说清缺什么。"""

    def test_defaults_do_not_raise(self) -> None:
        settings = Settings()
        ctx = DoctorContext(
            settings=settings,
            http_get=lambda url, timeout: (_ for _ in ()).throw(OSError("connection refused")),
            napcat_factory=lambda *args, **kw: _FakeNapCat(error=NapCatError("请求失败: refused")),
            open_store=lambda: self.store,
            python_version=(3, 12, 0),
        )
        checks = run_checks(ctx)
        self.assertEqual(worst_status(checks), FAIL)
        names = {item.name: item for item in checks}
        self.assertEqual(names["群白名单"].status, FAIL)
        self.assertEqual(names["推送通道"].status, FAIL)
        self.assertEqual(names["接收服务"].status, WARN)
        self.assertEqual(names["配置文件"].status, WARN)
        # 未配置 LLM 只是提醒，不是错误
        self.assertEqual(names["大模型"].status, WARN)

    def test_web_exposed_without_token_is_fail(self) -> None:
        settings = _settings(web_host="0.0.0.0", web_token="")
        self.assertEqual(check_web(self.context(settings)).status, FAIL)

    def test_web_exposed_with_store_token_passes(self) -> None:
        self.store.meta_set("web_token", WEB_TOKEN)
        settings = _settings(web_host="0.0.0.0", web_token="")
        self.assertEqual(check_web(self.context(settings)).status, OK)

    def test_storage_failure_is_reported_not_raised(self) -> None:
        def broken_store():
            raise OSError("disk full")

        check = check_storage(self.context(open_store=broken_store))
        self.assertEqual(check.status, FAIL)
        self.assertIn("disk full", check.detail)

    def test_python_below_312_is_warn(self) -> None:
        self.assertEqual(check_python(self.context(python_version=(3, 11, 9))).status, WARN)


class ComponentCheckTest(DoctorFixture):
    def test_napcat_token_mismatch_is_fail(self) -> None:
        factory = lambda *args, **kw: _FakeNapCat(  # noqa: E731
            error=NapCatError('get_status HTTP 403: {"message":"token verify failed!"}')
        )
        check = check_napcat(self.context(napcat_factory=factory))
        self.assertEqual(check.status, FAIL)
        self.assertIn("NAPCAT_API_TOKEN", check.hint)

    def test_napcat_unreachable_is_warn(self) -> None:
        factory = lambda *args, **kw: _FakeNapCat(error=NapCatError("get_status 请求失败: refused"))  # noqa: E731
        self.assertEqual(check_napcat(self.context(napcat_factory=factory)).status, WARN)

    def test_napcat_not_logged_in_is_warn(self) -> None:
        factory = lambda *args, **kw: _FakeNapCat(status={"data": {"online": False, "good": False}})  # noqa: E731
        check = check_napcat(self.context(napcat_factory=factory))
        self.assertEqual(check.status, WARN)

    def test_napcat_login_id_is_masked(self) -> None:
        check = check_napcat(self.context())
        self.assertNotIn("3566526103", check.detail)
        self.assertIn("35***03", check.detail)

    def test_receiver_down_is_warn_not_fail(self) -> None:
        ctx = self.context(http_get=lambda url, timeout: (_ for _ in ()).throw(OSError("refused")))
        self.assertEqual(check_receiver(ctx).status, WARN)

    def test_receiver_foreign_service_is_warn(self) -> None:
        ctx = self.context(http_get=lambda url, timeout: (200, json.dumps({"service": "other"})))
        self.assertEqual(check_receiver(ctx).status, WARN)

    def test_onebot_without_token_is_warn(self) -> None:
        self.assertEqual(check_onebot(self.context(_settings(onebot_token=""))).status, WARN)

    def test_onebot_disabled_is_ok(self) -> None:
        self.assertEqual(check_onebot(self.context(_settings(onebot_enabled=False))).status, OK)

    def test_groups_empty_is_fail_and_nonempty_is_ok(self) -> None:
        self.assertEqual(check_groups(self.context(_settings(group_whitelist=()))).status, FAIL)
        self.assertEqual(check_groups(self.context()).status, OK)

    def test_push_channels_empty_is_fail(self) -> None:
        settings = _settings(wxpusher_app_token="", wxpusher_uids=[])
        self.assertEqual(check_push(self.context(settings)).status, FAIL)

    def test_llm_without_key_is_warn_only_when_enabled(self) -> None:
        enabled = _settings(dashscope_api_key="", llm_enabled=True)
        self.assertEqual(check_llm(self.context(enabled)).status, WARN)
        disabled = _settings(dashscope_api_key="", llm_enabled=False, attachments_enabled=False)
        self.assertEqual(check_llm(self.context(disabled)).status, OK)

    def test_env_file_missing_is_warn(self) -> None:
        self.assertEqual(check_env_file(self.context(_settings(env_file=None))).status, WARN)

    def test_access_layer_reports_every_listener(self) -> None:
        check = check_access(self.context(_settings(web_host="0.0.0.0")))
        self.assertIn("OneBot", check.detail)
        self.assertIn("待办台", check.detail)
        self.assertEqual(check.status, OK)  # 有 token 且私有网络访问是允许的
        noisy = check_access(self.context(_settings(onebot_host="0.0.0.0", web_host="0.0.0.0")))
        self.assertEqual(noisy.status, WARN)


class HelperTest(unittest.TestCase):
    def test_loopback_detection(self) -> None:
        for host in ("127.0.0.1", "localhost", "::1", ""):
            self.assertTrue(_is_loopback(host))
        for host in ("0.0.0.0", "192.168.1.10", "10.0.0.5"):
            self.assertFalse(_is_loopback(host))

    def test_health_host_replaces_wildcard(self) -> None:
        self.assertEqual(_health_host("0.0.0.0"), "127.0.0.1")
        self.assertEqual(_health_host("::"), "127.0.0.1")
        self.assertEqual(_health_host("192.168.1.10"), "192.168.1.10")

    def test_worst_status_picks_most_severe(self) -> None:
        checks = [Check("a", OK, ""), Check("b", WARN, ""), Check("c", OK, "")]
        self.assertEqual(worst_status(checks), WARN)
        self.assertEqual(worst_status(checks + [Check("d", FAIL, "")]), FAIL)
        self.assertEqual(worst_status([]), OK)

    def test_render_includes_hint_line(self) -> None:
        text = render([Check("x", FAIL, "坏了", "去修")])
        self.assertIn("→ 去修", text)
        self.assertIn("1 项需处理", text)


class BotDependencyTest(DoctorFixture):
    """官方机器人的依赖与凭证检查（对应 2026-10-08 安全审计发现的 qq-botpy 缺失问题）。"""

    def _bot_settings(self) -> Settings:
        return _settings(appid="1234567890", secret="secret-value")

    def test_credentials_without_botpy_is_fail(self) -> None:
        original = doctor_module.botpy_available
        doctor_module.botpy_available = lambda: False
        self.addCleanup(setattr, doctor_module, "botpy_available", original)
        check = check_bot_credentials(self.context(self._bot_settings()))
        self.assertEqual(check.status, FAIL)
        self.assertIn("qq-botpy", check.hint)

    def test_credentials_with_botpy_is_ok(self) -> None:
        original = doctor_module.botpy_available
        doctor_module.botpy_available = lambda: True
        self.addCleanup(setattr, doctor_module, "botpy_available", original)
        check = check_bot_credentials(self.context(self._bot_settings()))
        self.assertEqual(check.status, OK)
        self.assertIn("qq-botpy", check.detail)

    def test_unconfigured_bot_is_ok_without_online_check(self) -> None:
        check = check_bot_credentials(self.context())
        self.assertEqual(check.status, OK)
        self.assertIn("未做在线校验", check.detail)


if __name__ == "__main__":
    unittest.main()
