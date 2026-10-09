"""多端推送扩展（Roadmap A14）回归测试。

守住：5 个新通道（ntfy / Telegram / Discord / 企业微信 / 邮件）的请求目标与正文正确、
异常统一抛 PushError、Settings 能按 env 配置它们、build_pushers 会按配置装配。
不联网：HTTP 与 SMTP 全部打桩。
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest import push  # noqa: E402
from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.push import (  # noqa: E402
    DiscordPusher,
    EmailPusher,
    NtfyPusher,
    PushError,
    TelegramPusher,
    WeComPusher,
    build_pushers,
)


class NtfyTest(unittest.TestCase):
    def test_posts_body_with_ascii_title_header(self) -> None:
        with mock.patch.object(push, "post_text", return_value="") as post:
            NtfyPusher("https://ntfy.sh", "mytopic", "tok").send("Hello", "正文内容")
        url, body, headers, _ = post.call_args.args
        self.assertEqual(url, "https://ntfy.sh/mytopic")
        self.assertIn("正文内容", body)
        self.assertEqual(headers["Title"], "Hello")
        self.assertEqual(headers["Authorization"], "Bearer tok")

    def test_non_ascii_title_goes_into_body_only(self) -> None:
        with mock.patch.object(push, "post_text", return_value="") as post:
            NtfyPusher("https://ntfy.example", "t").send("学院通知", "正文")
        _, body, headers, _ = post.call_args.args
        self.assertIn("学院通知", body)
        self.assertNotIn("Title", headers)
        self.assertEqual(post.call_args.args[0], "https://ntfy.example/t")


class TelegramTest(unittest.TestCase):
    def test_posts_to_bot_api(self) -> None:
        with mock.patch.object(push, "post_json", return_value={"ok": True}) as post:
            TelegramPusher("TOKEN", "12345").send("标题", "正文")
        url = post.call_args.args[0]
        payload = post.call_args.args[1]
        self.assertTrue(url.endswith("/botTOKEN/sendMessage"))
        self.assertEqual(payload["chat_id"], "12345")
        self.assertIn("正文", payload["text"])

    def test_error_response_raises(self) -> None:
        with mock.patch.object(push, "post_json", return_value={"ok": False, "description": "bad"}):
            with self.assertRaises(PushError):
                TelegramPusher("TOKEN", "1").send("标题", "正文")


class DiscordTest(unittest.TestCase):
    def test_posts_content_and_hides_url(self) -> None:
        with mock.patch.object(push, "post_json", return_value={}) as post:
            pusher = DiscordPusher("https://discord.com/api/webhooks/1/secret-token")
            pusher.send("标题", "正文")
        payload = post.call_args.args[1]
        self.assertIn("标题", payload["content"])
        self.assertNotIn("secret-token", pusher.target)


class WeComTest(unittest.TestCase):
    def test_success_when_errcode_zero(self) -> None:
        with mock.patch.object(push, "post_json", return_value={"errcode": 0}) as post:
            WeComPusher("my-key").send("标题", "正文")
        self.assertIn("key=my-key", post.call_args.args[0])
        self.assertEqual(post.call_args.args[1]["msgtype"], "text")

    def test_error_errcode_raises(self) -> None:
        with mock.patch.object(push, "post_json", return_value={"errcode": 93000, "errmsg": "invalid"}):
            with self.assertRaises(PushError):
                WeComPusher("my-key").send("标题", "正文")


class EmailTest(unittest.TestCase):
    def test_uses_ssl_on_465_and_sends(self) -> None:
        server = mock.MagicMock()
        with mock.patch.object(push.smtplib, "SMTP_SSL", return_value=server) as ssl_factory:
            EmailPusher(
                "smtp.example", 465, user="me@example", password="pw", recipients=["a@example"]
            ).send("标题", "正文")
        ssl_factory.assert_called_once()
        self.assertTrue(server.send_message.called)
        message = server.send_message.call_args.args[0]
        self.assertEqual(message["To"], "a@example")
        self.assertIn("正文", message.get_content())

    def test_plain_port_uses_starttls(self) -> None:
        server = mock.MagicMock()
        with mock.patch.object(push.smtplib, "SMTP", return_value=server) as plain_factory:
            EmailPusher("smtp.example", 587, recipients=["a@example"], starttls=True).send("t", "b")
        plain_factory.assert_called_once()
        self.assertTrue(server.starttls.called)

    def test_smtp_error_raises_push_error(self) -> None:
        with mock.patch.object(push.smtplib, "SMTP_SSL", side_effect=OSError("boom")):
            with self.assertRaises(PushError):
                EmailPusher("smtp.example", 465, recipients=["a@example"]).send("t", "b")


class ConfigTest(unittest.TestCase):
    def test_channels_listed_and_built(self) -> None:
        settings = Settings.from_env(
            env={
                "NTFY_TOPICS": "topic1,topic2",
                "QQ_DIGEST_NTFY_URL": "https://ntfy.example",
                "TELEGRAM_BOT_TOKEN": "T",
                "TELEGRAM_CHAT_IDS": "1,2",
                "QQ_DIGEST_DISCORD_WEBHOOKS": "https://discord.com/api/webhooks/1/x",
                "QQ_DIGEST_WECOM_KEYS": "abc",
                "QQ_DIGEST_SMTP_HOST": "smtp.example",
                "QQ_DIGEST_SMTP_PORT": "465",
                "QQ_DIGEST_SMTP_USER": "me@example",
                "QQ_DIGEST_SMTP_PASSWORD": "pw",
                "QQ_DIGEST_MAIL_TO": "a@example,b@example",
            }
        )
        for channel in ("ntfy", "telegram", "discord", "wecom", "email"):
            self.assertIn(channel, settings.push_channels())
        names = [pusher.name for pusher in build_pushers(settings)]
        self.assertEqual(sorted(names), ["discord", "email", "ntfy", "ntfy", "telegram", "telegram", "wecom"])

    def test_single_http_channel_becomes_primary(self) -> None:
        # 只有一个通道、又没有 QQ 通道时，它就是主通道（tier 0），不用等回退
        settings = Settings.from_env(env={"NTFY_TOPICS": "t"})
        pushers = build_pushers(settings)
        self.assertEqual([pusher.name for pusher in pushers], ["ntfy"])
        self.assertEqual(pushers[0].tier, 0)

    def test_absent_by_default(self) -> None:
        settings = Settings.from_env(env={"QQ_DIGEST_GROUPS": "1"})
        self.assertEqual(settings.ntfy_url, "https://ntfy.sh")
        for channel in ("ntfy", "telegram", "discord", "wecom", "email"):
            self.assertNotIn(channel, settings.push_channels())


if __name__ == "__main__":
    unittest.main()
