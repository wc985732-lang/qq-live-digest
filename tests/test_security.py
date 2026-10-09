"""安全回归测试（Roadmap A28：安全边界强化）。

本文件按**威胁模型**组织，只收录有独立价值的安全边界行为；
`tests/test_attachments.py::DownloadSecurityTest` 已覆盖的下载防护分支（协议白名单、
本机/内网/元数据 IP、userinfo 伪装、重定向逐跳校验、大小上限、HTTP 错误码）此处不重复。

约定：任何一个用例失败，都意味着一道安全边界被去掉或改坏了，必须先修复再合并。
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import socket
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest import receiver as receiver_module  # noqa: E402
from qq_live_digest.attachments import (  # noqa: E402
    DOC_CHUNK_CHARS,
    DOC_MAX_CHUNKS,
    MAX_ZIP_ENTRIES,
    MAX_ZIP_TOTAL_BYTES,
    AttachmentError,
    download,
    extract_text,
    extract_zip,
)
from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.receiver import OneBotReceiver  # noqa: E402
from qq_live_digest.store import Store  # noqa: E402

from qq_live_digest.webapp import TaskWebServer  # noqa: E402

TOKEN = "secret-token"

GROUP_EVENT = {
    "post_type": "message",
    "message_type": "group",
    "message_id": 2048,
    "group_id": 123456,
    "user_id": 999,
    "self_id": 111,
    "time": 1789000000,
    "sender": {"card": "辅导员", "nickname": "老师"},
    "message": [{"type": "text", "data": {"text": " 请今天18:00前提交材料 "}}],
}


def _raw_request(port: int, request: bytes) -> tuple[int, bytes]:
    """发送裸 HTTP 请求并返回 (状态码, 响应体)；用于构造 urllib 不方便发的畸形请求。"""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(request)
        data = b""
        while True:
            try:
                part = connection.recv(4096)
            except (ConnectionResetError, ConnectionAbortedError):
                # 服务端未读完请求体就关闭连接时，Windows 会回 RST：视为读取结束
                break
            if not part:
                break
            data += part
    head, _, body = data.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].split(b" ")
    return int(status_line[1]), body


class _FakeResponse:
    """仅供 download() 的 _transport 注入口，避免真实网络。"""

    def __init__(self, status: int = 200, headers: dict[str, str] | None = None, chunks=()):
        self.status = status
        self._headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        self._chunks = list(chunks)
        self.closed = False

    def getheader(self, name: str) -> str | None:
        return self._headers.get(name.lower())

    def read(self, size: int = -1) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------- 接收端：请求体上限
class OneBotBodyLimitTest(unittest.TestCase):
    """超限或畸形的请求体必须在进入业务逻辑之前被拒绝。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.received: list[dict] = []
        self.settings = Settings(
            group_whitelist=("123456",),
            onebot_enabled=True,
            onebot_host="127.0.0.1",
            onebot_port=0,
            onebot_token=TOKEN,
        )
        self.receiver = OneBotReceiver(self.settings, self._on_message)
        self.receiver.start()
        self.addCleanup(self.receiver.stop)
        assert self.receiver.server is not None
        self.port = self.receiver.server.server_address[1]

    def _on_message(self, record: dict) -> bool:
        self.received.append(record)
        return True

    def _headers(self, *, extra: bytes = b"") -> bytes:
        return (
            b"POST /onebot/event HTTP/1.1\r\n"
            + f"Host: 127.0.0.1:{self.port}\r\n".encode("ascii")
            + b"Content-Type: application/json\r\n"
            + f"Authorization: Bearer {TOKEN}\r\n".encode("ascii")
            + extra
            + b"Connection: close\r\n\r\n"
        )

    def test_oversized_content_length_is_rejected(self) -> None:
        limit = receiver_module.MAX_BODY_BYTES
        request = self._headers(
            extra=f"Content-Length: {limit + 1}\r\n".encode("ascii")
        )
        status, _ = _raw_request(self.port, request)
        self.assertEqual(status, 400)
        self.assertEqual(self.received, [])
        self.assertEqual(self.receiver.accepted, 0)

    def test_oversized_chunked_body_is_rejected(self) -> None:
        limit = receiver_module.MAX_BODY_BYTES
        request = (
            self._headers(extra=b"Transfer-Encoding: chunked\r\n")
            + f"{limit + 1:x}\r\n".encode("ascii")
        )
        status, _ = _raw_request(self.port, request)
        self.assertEqual(status, 400)
        self.assertEqual(self.received, [])

    def test_malformed_chunk_size_is_rejected(self) -> None:
        request = self._headers(extra=b"Transfer-Encoding: chunked\r\n") + b"zz\r\n"
        status, _ = _raw_request(self.port, request)
        self.assertEqual(status, 400)
        self.assertEqual(self.received, [])

    def test_body_within_limit_is_still_accepted(self) -> None:
        """反向对照：正常体积的合法请求必须照常处理，避免限流误伤。"""
        body = json.dumps(GROUP_EVENT, ensure_ascii=False).encode("utf-8")
        request = self._headers(extra=f"Content-Length: {len(body)}\r\n".encode("ascii")) + body
        status, payload = _raw_request(self.port, request)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(payload.decode("utf-8"))["inserted"])
        self.assertEqual(len(self.received), 1)


# --------------------------------------------------------------- 接收端：鉴权
class OneBotAuthTest(unittest.TestCase):
    """鉴权失败时：返回 403，且请求体绝不进入消息处理链路。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.received: list[dict] = []
        self.settings = Settings(
            group_whitelist=("123456",),
            onebot_enabled=True,
            onebot_host="127.0.0.1",
            onebot_port=0,
            onebot_token=TOKEN,
        )
        self.receiver = OneBotReceiver(self.settings, self._on_message)
        self.receiver.start()
        self.addCleanup(self.receiver.stop)
        assert self.receiver.server is not None
        self.port = self.receiver.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/onebot/event"
        self.body = json.dumps(GROUP_EVENT, ensure_ascii=False).encode("utf-8")

    def _on_message(self, record: dict) -> bool:
        self.received.append(record)
        return True

    def _post(self, headers: dict[str, str]) -> None:
        request = urllib.request.Request(self.url, data=self.body, headers=headers)
        urllib.request.urlopen(request, timeout=5)

    def test_wrong_token_is_rejected_without_dispatch(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            self._post({"Content-Type": "application/json", "Authorization": "Bearer wrong-token"})
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.received, [])
        self.assertEqual(self.receiver.accepted, 0)

    def test_missing_token_is_rejected(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            self._post({"Content-Type": "application/json"})
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.received, [])

    def test_token_prefix_is_not_accepted(self) -> None:
        """token 必须精确匹配，不能是前缀/超串匹配。"""
        with self.assertRaises(urllib.error.HTTPError) as error:
            self._post({"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}-extra"})
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.received, [])

    def test_signature_must_match_the_body(self) -> None:
        """签名只对当前请求体有效，换一个 body 的签名必须失败。"""
        other = json.dumps({**GROUP_EVENT, "group_id": 654321}).encode("utf-8")
        digest = hmac.new(TOKEN.encode("utf-8"), other, hashlib.sha1).hexdigest()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self._post({"Content-Type": "application/json", "X-Signature": f"sha1={digest}"})
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.received, [])

    def test_valid_token_is_accepted(self) -> None:
        """反向对照：正确 token 必须被接受。"""
        self._post({"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(len(self.received), 1)


# --------------------------------------------------------------- 接收端：对外暴露面
class OneBotExposureTest(unittest.TestCase):
    """/health 必须免鉴权可用，但不能泄露 token 等敏感信息；其他路径一律 404。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(
            group_whitelist=("123456",),
            onebot_enabled=True,
            onebot_host="127.0.0.1",
            onebot_port=0,
            onebot_token=TOKEN,
        )
        self.receiver = OneBotReceiver(self.settings, lambda record: True)
        self.receiver.health_provider = lambda: {"catchup_enabled": False}
        self.receiver.start()
        self.addCleanup(self.receiver.stop)
        assert self.receiver.server is not None
        self.base = f"http://127.0.0.1:{self.receiver.server.server_address[1]}"

    def test_health_is_available_without_token_and_does_not_leak(self) -> None:
        with urllib.request.urlopen(self.base + "/health", timeout=5) as response:
            raw = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["service"], "qq-live-digest")
        self.assertNotIn(TOKEN, raw)

    def test_health_stays_available_when_provider_fails(self) -> None:
        def boom() -> dict:
            raise RuntimeError("provider down")

        self.receiver.health_provider = boom
        with urllib.request.urlopen(self.base + "/health", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])

    def test_unknown_paths_return_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.base + "/admin", timeout=5)
        self.assertEqual(error.exception.code, 404)


# --------------------------------------------------------------- 下载：SSRF 补充分支
class DownloadGuardTest(unittest.TestCase):
    """补充 DownloadSecurityTest 未覆盖的拒绝分支。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name) / "a.pdf"

    def test_ipv4_mapped_ipv6_loopback_is_rejected(self) -> None:
        """::ffff:127.0.0.1 这类映射地址不能绕过内网判定。"""
        with self.assertRaises(AttachmentError):
            download(
                "http://evil.example/a.pdf",
                self.dest,
                max_bytes=1024,
                _resolver=lambda host, port: ["::ffff:127.0.0.1"],
            )
        self.assertFalse(self.dest.exists())

    def test_dns_failure_becomes_attachment_error(self) -> None:
        def boom(host: str, port: int) -> list[str]:
            raise OSError("dns down")

        with self.assertRaises(AttachmentError):
            download("http://evil.example/a.pdf", self.dest, max_bytes=1024, _resolver=boom)

    def test_hostname_resolving_to_nothing_is_rejected(self) -> None:
        with self.assertRaises(AttachmentError):
            download(
                "http://evil.example/a.pdf",
                self.dest,
                max_bytes=1024,
                _resolver=lambda host, port: [],
            )

    def test_empty_response_body_is_rejected(self) -> None:
        with self.assertRaises(AttachmentError):
            download(
                "http://93.184.216.34/a.pdf",
                self.dest,
                max_bytes=1024,
                _transport=lambda target, timeout: _FakeResponse(200, {}, []),
            )
        self.assertFalse(self.dest.exists())

    def test_truncated_download_is_rejected_without_partial_file(self) -> None:
        """声明长度与实际字节数不符（被截断）时必须拒绝，且不留 .part 半成品。"""

        def broken(target, timeout):
            return _FakeResponse(200, {"Content-Length": "10"}, [b"0123", b""])

        with self.assertRaises(AttachmentError):
            download("http://93.184.216.34/a.pdf", self.dest, max_bytes=1024, _transport=broken)
        leftovers = list(Path(self.tmp.name).glob("*"))
        self.assertEqual(leftovers, [])


# --------------------------------------------------------------- 附件：压缩炸弹
class AttachmentBombTest(unittest.TestCase):
    """压缩包必须限制单条目大小、总条目数与嵌套层数，且坏文件不能抛异常。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _path(self, name: str) -> Path:
        return Path(self.tmp.name) / name

    def test_member_exceeding_total_limit_is_skipped(self) -> None:
        path = self._path("bomb.zip")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("big.txt", "A" * (MAX_ZIP_TOTAL_BYTES + 1))
        self.assertEqual(extract_zip(path), "")

    def test_entry_count_is_capped(self) -> None:
        path = self._path("many.zip")
        with zipfile.ZipFile(path, "w") as archive:
            for index in range(MAX_ZIP_ENTRIES + 3):
                archive.writestr(f"f{index:02d}.txt", f"内容{index}")
        result = extract_zip(path)
        self.assertEqual(result.count("【压缩包内文件"), MAX_ZIP_ENTRIES)
        self.assertIn("压缩包共", result)

    def test_nested_archive_is_not_expanded(self) -> None:
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as archive:
            archive.writestr("x.txt", "内层机密")
        path = self._path("nested.zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("inner.zip", inner.getvalue())
            archive.writestr("note.txt", "外层正常")
        result = extract_zip(path)
        self.assertIn("外层正常", result)
        self.assertNotIn("内层机密", result)

    def test_extraction_output_is_bounded(self) -> None:
        """即使条目都在单条目上限内，累计输出也不能无限增长。"""
        path = self._path("wide.zip")
        chunk = "B" * (DOC_CHUNK_CHARS // 2)
        with zipfile.ZipFile(path, "w") as archive:
            for index in range(MAX_ZIP_ENTRIES):
                archive.writestr(f"c{index:02d}.txt", chunk)
        result = extract_zip(path)
        self.assertLessEqual(len(result), DOC_CHUNK_CHARS * DOC_MAX_CHUNKS + len(chunk) + 512)

    def test_corrupt_archive_is_not_fatal(self) -> None:
        """群友发来的损坏压缩包只能被跳过，不能让解析线程抛异常。"""
        path = self._path("broken.zip")
        path.write_bytes(b"PK\x03\x04 not really a zip")
        self.assertEqual(extract_text(path), "")


# --------------------------------------------------------------- 移动端待办台
class MobileApiSecurityTest(unittest.TestCase):
    """PWA 资源公开，但任务数据必须鉴权；写操作未授权时不得改变状态。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.token = "s3cr3t-token-xyz"
        self.store = Store(Path(self.tmp.name) / "sec.sqlite3")
        self.task_id = self.store.upsert_task(
            task_key="sec-1",
            summary="提交实验报告",
            category="action",
            deadline=None,
            groups=["测仪2602班群"],
            evidence="请今天18:00前提交实验报告",
        )
        self.settings = Settings(
            group_whitelist=("g1",),
            web_host="127.0.0.1",
            web_port=0,
            web_token=self.token,
        )
        self.server = TaskWebServer(self.settings, self.store)
        self.assertTrue(self.server.start())
        self.addCleanup(self.server.stop)
        assert self.server.server is not None
        self.base = f"http://127.0.0.1:{self.server.server.server_address[1]}"

    def test_task_data_requires_token(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.base + "/api/tasks", timeout=5)
        self.assertEqual(error.exception.code, 401)

    def _post_without_auth(self) -> int:
        """未授权 POST 必须返回 401。

        服务端在鉴权之前不读取请求体，直接关闭连接；Windows 下这会产生 RST，
        客户端可能先拿到 ConnectionResetError 而不是 401。两种情况都算“被拒绝”，
        但要额外确认服务仍然健康，排除“其实是崩溃了”的假阳性。
        """
        request = urllib.request.Request(
            f"{self.base}/api/tasks/{self.task_id}",
            data=json.dumps({"action": "done"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5):
                return 200
        except urllib.error.HTTPError as error:
            return int(error.code)
        except (ConnectionResetError, ConnectionAbortedError):
            with urllib.request.urlopen(self.base + "/api/health", timeout=5) as health:
                self.assertEqual(health.status, 200)
            return 401

    def test_write_without_token_does_not_change_state(self) -> None:
        self.assertEqual(self._post_without_auth(), 401)
        self.assertNotEqual(self.store.get_task(self.task_id)["status"], "done")

    def test_write_with_token_succeeds(self) -> None:
        """反向对照：带正确 token 的写操作必须生效。"""
        request = urllib.request.Request(
            f"{self.base}/api/tasks/{self.task_id}",
            data=json.dumps({"action": "done"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Token": self.token},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(self.store.get_task(self.task_id)["status"], "done")

    def test_health_is_public(self) -> None:
        with urllib.request.urlopen(self.base + "/api/health", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])

    def test_task_page_does_not_echo_the_token(self) -> None:
        with urllib.request.urlopen(f"{self.base}/?token={self.token}", timeout=5) as response:
            html = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertNotIn(self.token, html)


class LoopbackAuthTest(unittest.TestCase):
    """回环部署也必须鉴权：曾经的实现「绑 127.0.0.1 就不生成 token」是安全边界反转。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "loop.sqlite3")
        self.task_id = self.store.upsert_task(task_key="l1", summary="交表", category="action")
        self.settings = Settings(web_host="127.0.0.1", web_port=0, web_token="")
        self.server = TaskWebServer(self.settings, self.store)
        self.assertTrue(self.server.token, "回环监听也必须自动生成 token")
        self.assertTrue(self.server.start())
        self.addCleanup(self.server.stop)
        assert self.server.server is not None
        self.base = f"http://127.0.0.1:{self.server.server.server_address[1]}"

    def test_loopback_without_configured_token_is_not_open(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.base + "/api/tasks", timeout=5)
        self.assertEqual(error.exception.code, 401)

    def _post(self, *, origin: str = "") -> int:
        headers = {"Content-Type": "application/json", "X-Token": self.server.token}
        if origin:
            headers["Origin"] = origin
        request = urllib.request.Request(
            f"{self.base}/api/tasks/{self.task_id}",
            data=json.dumps({"action": "done"}).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return int(response.status)
        except urllib.error.HTTPError as error:
            return int(error.code)

    def test_cross_site_write_is_rejected(self) -> None:
        self.assertEqual(self._post(origin="https://evil.example"), 403)
        self.assertNotEqual(self.store.get_task(self.task_id)["status"], "done")

    def test_same_origin_write_is_allowed(self) -> None:
        self.assertEqual(self._post(origin=self.base), 200)
        self.assertEqual(self.store.get_task(self.task_id)["status"], "done")


if __name__ == "__main__":
    unittest.main()
