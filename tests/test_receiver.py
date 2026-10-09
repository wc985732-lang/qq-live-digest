from __future__ import annotations

import hashlib
import hmac

import json
import socket
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.receiver import OneBotReceiver, flatten_message, parse_onebot_event  # noqa: E402

GROUP_EVENT = {
    "post_type": "message",
    "message_type": "group",
    "message_id": 1024,
    "group_id": 123456,
    "user_id": 999,
    "self_id": 111,
    "time": 1789000000,
    "sender": {"card": "辅导员", "nickname": "老师"},
    "message": [
        {"type": "at", "data": {"qq": "all", "name": "全体成员"}},
        {"type": "text", "data": {"text": " 请今天18:00前提交材料 "}},
        {"type": "image", "data": {"url": "http://example.com/a.png"}},
    ],
}


class OneBotParseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(group_whitelist=("123456",), group_aliases={"123456": "学院通知群"})

    def test_flatten_message(self) -> None:
        self.assertIn("@全体成员", flatten_message(GROUP_EVENT["message"]))
        self.assertIn("[图片]", flatten_message(GROUP_EVENT["message"]))
        self.assertEqual(flatten_message("纯文本"), "纯文本")

    def test_parse_group_event(self) -> None:
        record = parse_onebot_event(GROUP_EVENT, self.settings)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["msg_id"], "onebot:123456:1024")
        self.assertEqual(record["group_name"], "学院通知群")
        self.assertEqual(record["sender_name"], "辅导员")
        self.assertIn("18:00", record["content"])

    def test_ignore_non_group_and_self(self) -> None:
        self.assertIsNone(parse_onebot_event({"post_type": "message", "message_type": "private"}, self.settings))
        self.assertIsNone(
            parse_onebot_event({**GROUP_EVENT, "user_id": GROUP_EVENT["self_id"]}, self.settings)
        )
        self.assertIsNone(parse_onebot_event({"post_type": "notice"}, self.settings))

    def test_hash_id_when_message_id_missing(self) -> None:
        payload = {k: v for k, v in GROUP_EVENT.items() if k != "message_id"}
        record = parse_onebot_event(payload, self.settings)
        assert record is not None
        self.assertTrue(record["msg_id"].startswith("onebot:123456:hash-"))

    def test_raw_message_fallback(self) -> None:
        payload = {**GROUP_EVENT, "message": "", "raw_message": "CQ 原始文本"}
        record = parse_onebot_event(payload, self.settings)
        assert record is not None
        self.assertEqual(record["content"], "CQ 原始文本")


class OneBotReceiverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.received: list[dict] = []
        self.settings = Settings(
            group_whitelist=("123456",),
            onebot_enabled=True,
            onebot_host="127.0.0.1",
            onebot_port=0,
            onebot_token="secret-token",
        )
        self.receiver = OneBotReceiver(self.settings, self._on_message)

    def _on_message(self, record: dict) -> bool:
        self.received.append(record)
        return True

    def test_handle_payload_whitelist_and_dedupe(self) -> None:
        status, body = self.receiver.handle_payload(GROUP_EVENT)
        self.assertEqual(status, 200)
        self.assertTrue(body["inserted"])
        self.assertEqual(len(self.received), 1)

        outside = {**GROUP_EVENT, "group_id": 777}
        status, body = self.receiver.handle_payload(outside)
        self.assertEqual(status, 204)
        self.assertTrue(body["ignored"])
        self.assertEqual(len(self.received), 1)

    def test_http_server_auth(self) -> None:
        self.receiver.health_provider = lambda: {
            "catchup_enabled": True,
            "last_catchup_at": "2026-09-29T01:14:56+08:00",
        }
        self.receiver.start()
        self.addCleanup(self.receiver.stop)
        assert self.receiver.server is not None
        port = self.receiver.server.server_address[1]
        url = f"http://127.0.0.1:{port}/onebot/event"
        data = json.dumps(GROUP_EVENT).encode("utf-8")

        request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(error.exception.code, 403)

        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "Authorization": "Bearer secret-token"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(len(self.received), 1)

        health = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5)
        with health:
            health_payload = json.loads(health.read().decode("utf-8"))
        self.assertTrue(health_payload["ok"])
        self.assertTrue(health_payload["catchup_enabled"])
        self.assertEqual(health_payload["last_catchup_at"], "2026-09-29T01:14:56+08:00")


    def test_http_server_accepts_napcat_signature(self) -> None:
        """NapCat 上报不带 Authorization，只带 x-signature=sha1=<hmac_sha1(token, body)>。"""
        self.receiver.start()
        self.addCleanup(self.receiver.stop)
        assert self.receiver.server is not None
        port = self.receiver.server.server_address[1]
        url = f"http://127.0.0.1:{port}/onebot/event"
        data = json.dumps(GROUP_EVENT).encode("utf-8")
        digest = hmac.new(b"secret-token", data, hashlib.sha1).hexdigest()

        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "X-Signature": f"sha1={digest}"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["inserted"])
        self.assertEqual(len(self.received), 1)

        bad = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "X-Signature": "sha1=" + "0" * 40},
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(bad, timeout=5)
        self.assertEqual(error.exception.code, 403)

    def test_http_server_accepts_chunked_report(self) -> None:
        """NapCat may send reports with Transfer-Encoding: chunked."""
        self.receiver.start()
        self.addCleanup(self.receiver.stop)
        assert self.receiver.server is not None
        port = self.receiver.server.server_address[1]
        body = json.dumps(GROUP_EVENT, ensure_ascii=False).encode("utf-8")
        digest = hmac.new(b"secret-token", body, hashlib.sha1).hexdigest()
        chunked = b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body)
        request = (
            b"POST /onebot/event HTTP/1.1\r\n"
            + f"Host: 127.0.0.1:{port}\r\n".encode("ascii")
            + b"Content-Type: application/json\r\n"
            + b"Transfer-Encoding: chunked\r\n"
            + f"X-Signature: sha1={digest}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + chunked
        )
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            connection.sendall(request)
            response = b""
            while True:
                part = connection.recv(4096)
                if not part:
                    break
                response += part
        header, _, payload = response.partition(b"\r\n\r\n")
        self.assertIn(b" 200 ", header.split(b"\r\n", 1)[0])
        parsed = json.loads(payload.decode("utf-8"))
        self.assertTrue(parsed["inserted"])
        self.assertEqual(len(self.received), 1)

    def test_refuses_non_loopback_without_token(self) -> None:
        settings = Settings(
            group_whitelist=("123456",),
            onebot_enabled=True,
            onebot_host="0.0.0.0",
            onebot_port=0,
            onebot_token="",
        )
        receiver = OneBotReceiver(settings, self._on_message)
        self.assertFalse(receiver.start())
        self.assertIsNone(receiver.server)


if __name__ == "__main__":
    unittest.main()
