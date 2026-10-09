"""OneBot v11 事件接收（NapCat / LLOneBot 等兜底方案）。

NapCat 的 HTTP 上报地址填：http://127.0.0.1:8765/onebot/event
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .config import Settings
from .timeutil import iso, now_local

from .attachments import attachment_from_notice, attachments_from_message

LOGGER = logging.getLogger(__name__)

MAX_BODY_BYTES = 4 * 1024 * 1024

SEGMENT_LABEL = {
    "image": "[图片]",
    "face": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "forward": "[合并转发]",
    "json": "[卡片消息]",
    "file": "[文件]",
    "mface": "[表情]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
}


def flatten_message(message: Any) -> str:
    if isinstance(message, str):
        return message.strip()
    parts: list[str] = []
    for segment in message or []:
        if not isinstance(segment, dict):
            parts.append(str(segment))
            continue
        kind = str(segment.get("type") or "")
        data = segment.get("data") or {}
        if kind == "text":
            parts.append(str(data.get("text") or ""))
        elif kind == "at":
            name = data.get("name") or data.get("qq") or "某人"
            parts.append(f"@{name}")
        elif kind == "reply":
            continue
        elif kind == "file":
            name = str(data.get("file") or data.get("file_name") or "").strip()
            parts.append(f"[文件]{name}" if name else "[文件]")
        else:
            parts.append(SEGMENT_LABEL.get(kind, f"[{kind}]"))
    return "".join(parts).strip()


def parse_onebot_event(payload: Any, settings: Settings) -> dict[str, Any] | None:
    """把 OneBot v11 群消息事件转成统一的消息记录；非群消息返回 None。"""
    if not isinstance(payload, dict):
        return None
    if str(payload.get("post_type") or "") != "message":
        return None
    if str(payload.get("message_type") or "") != "group":
        return None

    user_id = str(payload.get("user_id") or "")
    if user_id and user_id == str(payload.get("self_id") or ""):
        return None

    group_id = str(payload.get("group_id") or "")
    if not group_id:
        return None

    content = flatten_message(payload.get("message"))
    if not content:
        content = flatten_message(payload.get("raw_message"))
    if not content:
        return None

    sender = payload.get("sender") or {}
    sender_name = str(sender.get("card") or sender.get("nickname") or user_id or "群成员")
    message_id = str(payload.get("message_id") or "")
    if not message_id:
        digest = hashlib.sha1(f"{group_id}:{user_id}:{payload.get('time')}:{content}".encode("utf-8"))
        message_id = "hash-" + digest.hexdigest()[:16]

    return {
        "msg_id": f"onebot:{group_id}:{message_id}",
        "source": "onebot",
        "event": "GROUP_MESSAGE_CREATE",
        "group_id": group_id,
        "group_name": settings.group_name(group_id),
        "sender_id": user_id,
        "sender_name": sender_name,
        "ts": iso(payload.get("time")) or iso(now_local()),
        "received_at": iso(now_local()),
        "content": content,
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "qq-live-digest/1.0"
    on_event: Callable[[Any], tuple[int, dict[str, Any]]]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 与基类签名一致
        LOGGER.debug("onebot %s", format % args)

    def _authorized(self) -> bool:
        token = getattr(self.server, "token", "")
        if not token:
            return True
        expected = token.encode("utf-8")
        header = self.headers.get("Authorization", "")
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        supplied = params.get("access_token", [])
        if any(hmac.compare_digest(str(item).encode("utf-8"), expected) for item in supplied):
            return True
        candidate = header[7:].strip() if header.startswith("Bearer ") else header.strip()
        if candidate and hmac.compare_digest(candidate.encode("utf-8"), expected):
            return True
        body = getattr(self, "_raw_body", b"")
        signature = self.headers.get("X-Signature", "").strip()
        if signature.lower().startswith("sha1="):
            signature_digest = hmac.new(token.encode("utf-8"), body, hashlib.sha1).hexdigest()
            if hmac.compare_digest(signature[5:].strip().lower(), signature_digest):
                return True
        names = ",".join(sorted(str(key).lower() for key in self.headers.keys()))
        LOGGER.warning(
            "OneBot 鉴权失败：path=%s header_len=%d scheme=%r candidate_len=%d candidate_sha1=%s signature=%r body_len=%d headers=%s",
            self.path,
            len(header),
            header.split(" ", 1)[0] if header else "",
            len(candidate),
            hashlib.sha1(candidate.encode("utf-8")).hexdigest()[:10],
            signature[:14],
            len(body),
            names,
        )
        return False

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        transfer = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer:
            chunks: list[bytes] = []
            total = 0
            while True:
                line = self.rfile.readline(65536)
                if not line:
                    raise ValueError("unexpected EOF while reading chunk size")
                size_token = line.strip().split(b";", 1)[0]
                size = int(size_token, 16)
                if size < 0 or size > MAX_BODY_BYTES:
                    raise ValueError("invalid chunk size")
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(65536)
                        if trailer in (b"", b"\r\n", b"\n"):
                            break
                    break
                total += size
                if total > MAX_BODY_BYTES:
                    raise ValueError("chunked body too large")
                chunk = self.rfile.read(size)
                if len(chunk) != size:
                    raise ValueError("unexpected EOF while reading chunk")
                chunks.append(chunk)
                terminator = self.rfile.read(2)
                if terminator not in (b"\r\n", b"\n"):
                    raise ValueError("invalid chunk terminator")
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("invalid content length")
        return self.rfile.read(length) if length else b""

    def do_GET(self) -> None:  # noqa: N802 - 基类命名
        if self.path.startswith("/health"):
            payload: dict[str, Any] = {"ok": True, "service": "qq-live-digest"}
            provider = getattr(self.server, "health", None)
            if callable(provider):
                try:
                    extra = provider()
                    if isinstance(extra, dict):
                        payload.update(extra)
                except Exception as error:  # noqa: BLE001 - health must stay available
                    LOGGER.warning("health provider failed: %s", error)
            self._respond(200, payload)
        else:
            self._respond(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - 基类命名
        try:
            raw = self._read_body()
        except (ValueError, OSError) as error:
            self._respond(400, {"ok": False, "error": f"bad body: {error}"})
            return
        self._raw_body = raw
        if not self._authorized():
            self._respond(403, {"ok": False, "error": "invalid token"})
            return
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except (ValueError, json.JSONDecodeError) as error:
            self._respond(400, {"ok": False, "error": f"bad json: {error}"})
            return
        status, response = self.server.on_event(payload)  # type: ignore[attr-defined]
        self._respond(status, response)


class OneBotReceiver:
    """最小 HTTP 服务器，只接受 OneBot 上报的群消息。"""

    def __init__(
        self,
        settings: Settings,
        on_message: Callable[[dict[str, Any]], Any],
        on_attachment: Callable[[Any], Any] | None = None,
        logger: logging.Logger | None = None,
        health_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.settings = settings
        self.on_message = on_message
        self.on_attachment = on_attachment
        self.logger = logger or LOGGER
        self.health_provider = health_provider
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.accepted = 0
        self.rejected = 0

    @property
    def is_alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def handle_payload(self, payload: Any) -> tuple[int, dict[str, Any]]:
        queued = self._dispatch_attachments(payload)
        record = parse_onebot_event(payload, self.settings)
        if not record:
            self.rejected += int(not queued)
            return 204, {"ok": True, "ignored": not queued, "attachments": queued}
        if not self.settings.accepts_group(record["group_id"]):
            self.rejected += 1
            self.logger.info("忽略白名单外的群：%s", record["group_id"])
            return 204, {"ok": True, "ignored": True}
        inserted = bool(self.on_message(record))
        self.accepted += int(inserted)
        return 200, {
            "ok": True,
            "inserted": inserted,
            "msg_id": record["msg_id"],
            "attachments": queued,
        }

    def _dispatch_attachments(self, payload: Any) -> int:
        """把群文件 / 群图片转交给后台队列；异常不影响消息入库。"""
        if not self.on_attachment or not self.settings.attachments_enabled:
            return 0
        try:
            candidates = attachments_from_message(payload, self.settings)
            notice = attachment_from_notice(payload, self.settings)
            if notice is not None:
                candidates.append(notice)
        except Exception:  # noqa: BLE001 - 解析失败不能影响主流程
            self.logger.exception("附件事件解析失败")
            return 0
        count = 0
        for attachment in candidates:
            try:
                count += int(bool(self.on_attachment(attachment)))
            except Exception:  # noqa: BLE001
                self.logger.exception("附件入队失败：%s", attachment.name)
        return count

    def start(self) -> bool:
        if self.is_alive:
            return False
        host = str(self.settings.onebot_host or "")
        if not str(self.settings.onebot_token or "") and host not in {"127.0.0.1", "localhost", "::1"}:
            self.logger.error(
                "拒绝启动 OneBot 接收器：监听 %s 但未设置 QQ_DIGEST_ONEBOT_TOKEN，"
                "任何能连上的人都能伪造群消息；请设 token 或只监听 127.0.0.1。",
                host,
            )
            return False
        server = ThreadingHTTPServer((self.settings.onebot_host, self.settings.onebot_port), _Handler)
        server.token = self.settings.onebot_token  # type: ignore[attr-defined]
        server.on_event = self.handle_payload  # type: ignore[attr-defined]
        server.health = self.health_provider  # type: ignore[attr-defined]
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, name="onebot-http", daemon=True)
        self.thread.start()
        self.logger.info(
            "OneBot 接收器已启动 http://%s:%d/onebot/event",
            self.settings.onebot_host,
            self.settings.onebot_port,
        )
        return True

    def stop(self, timeout: float = 5) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout)
        self.logger.info("OneBot 接收器已停止。")
