"""群文件 / 群图片：下载、解析、AI 摘要，再作为普通消息进入摘要流水线。

设计要点：
- 上报事件只做入队，下载和解析都在后台线程里做，避免阻塞 OneBot 回调。
- 解析结果拼成一条普通消息记录（``【文件】名称`` / ``【图片】``），
  后续筛选、分级、去重、推送全部复用原有逻辑。
- 只解析不执行；zip 只解白名单扩展名并限制解压总量。
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import ipaddress
import json
import logging
import queue
import re
import socket
import ssl
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import providers
from .config import Settings
from .retry import LLMError
from .timeutil import iso, now_local

LOGGER = logging.getLogger(__name__)

TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".log", ".htm", ".html"}
DOC_EXTS = {".docx", ".pdf", ".xlsx", ".xlsm", ".pptx"}
ARCHIVE_EXTS = {".zip"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
SUPPORTED_EXTS = TEXT_EXTS | DOC_EXTS | ARCHIVE_EXTS | IMAGE_EXTS
LEGACY_EXTS = {".doc", ".xls", ".ppt", ".wps", ".et", ".dps"}

MAX_ZIP_ENTRIES = 20
MAX_ZIP_TOTAL_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 40
MIN_IMAGE_BYTES = 10 * 1024
MIN_DOC_TEXT_CHARS = 40
MAX_RECORD_CHARS = 1500
DOC_CHUNK_CHARS = 4000
DOC_MAX_CHUNKS = 8
NO_TEXT_MARKERS = ("无有效通知", "无文字内容", "没有文字", "无文字")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) qq-live-digest/1.0"

SUMMARY_SYSTEM_PROMPT = (
    "你是大学生QQ群通知助手。只依据给定内容工作，不猜测、不补造，输出中文纯文本。"
)
DOC_PROMPT = (
    "这是群文件《{name}》的正文。请只依据正文提取与大学生有关的信息。\n"
    "输出严格 JSON，不要 Markdown、不要代码块、不要解释，格式：\n"
    "{{\"summary\":\"一句话摘要\",\"audience\":\"适用对象\",\"condition\":\"适用条件\","
    "\"details\":[\"按原文分条保留不同人群、例外、地点、附件、链接和注意事项\"],"
    "\"action\":\"需要做什么\",\"deadline\":\"YYYY-MM-DD HH:MM 或空字符串\"}}\n"
    "要求：summary 不超过80字；必须保留适用对象、条件、例外和不同人群差异；"
    "不要把部分同学的通知写成所有同学；没有相关信息的字段留空。\n\n{body}"
)
IMAGE_PROMPT = (
    "把图片里的文字原样提取出来，保留日期、时间、人名、金额、地点、群号等关键信息。"
    "如果图片里没有可读文字，或不含与学习、通知、表格、名单相关的内容，"
    "只回复：无有效通知。不要描述画面，不要解释。"
)


class AttachmentError(RuntimeError):
    """附件处理失败。"""


@dataclass
class Attachment:
    kind: str  # "file" | "image"
    name: str
    group_id: str = ""
    group_name: str = ""
    sender_id: str = ""
    sender_name: str = ""
    url: str = ""
    file_id: str = ""
    busid: str = ""
    size: int = 0
    ts: str = ""
    msg_id: str = ""
    hint: str = ""

    @property
    def suffix(self) -> str:
        return Path(self.name or "").suffix.lower()

    @property
    def key(self) -> str:
        # 只按群+类型+文件名+大小做键：群文件上传通知和随后的文件消息会得到同一个键，
        # 同一个文件不会被下载、摘要两次。
        seed = f"{self.group_id}|{self.kind}|{self.name}|{self.size}"
        return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]

    @property
    def size_text(self) -> str:
        size = int(self.size or 0)
        if size <= 0:
            return ""
        if size < 1024:
            return f"{size}B"
        if size < 1024 * 1024:
            return f"{size / 1024:.0f}KB"
        return f"{size / 1024 / 1024:.1f}MB"


# --------------------------------------------------------------------- 解析
def _is_sticker(segment: dict[str, Any]) -> bool:
    data = segment.get("data") or {}
    if str(data.get("sub_type") or "0") == "1":
        return True
    summary = str(data.get("summary") or "").strip()
    return bool(re.fullmatch(r"\[[^\[\]]{1,12}\]", summary))


def _int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def attachments_from_message(payload: Any, settings: Settings) -> list[Attachment]:
    """从一条群消息里挑出可处理的文件 / 图片。"""
    if not isinstance(payload, dict):
        return []
    if str(payload.get("post_type") or "") != "message":
        return []
    if str(payload.get("message_type") or "") != "group":
        return []

    group_id = str(payload.get("group_id") or "")
    if not group_id or not settings.accepts_group(group_id):
        return []
    user_id = str(payload.get("user_id") or "")
    if user_id and user_id == str(payload.get("self_id") or ""):
        return []

    message_id = str(payload.get("message_id") or "")
    sender = payload.get("sender") or {}
    sender = sender if isinstance(sender, dict) else {}
    sender_name = str(sender.get("card") or sender.get("nickname") or user_id or "群成员")
    stamp = iso(payload.get("time")) or iso(now_local())

    segments = payload.get("message")
    if not isinstance(segments, list):
        return []
    hint = "".join(
        str((seg.get("data") or {}).get("text") or "")
        for seg in segments
        if isinstance(seg, dict) and str(seg.get("type")) == "text"
    ).strip()

    found: list[Attachment] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        kind = str(segment.get("type") or "")
        data = segment.get("data") or {}
        if not isinstance(data, dict):
            continue
        if kind == "file":
            name = str(data.get("file") or data.get("file_name") or "").strip()
            if not name:
                continue
            found.append(
                Attachment(
                    kind="file",
                    name=name,
                    group_id=group_id,
                    group_name=settings.group_name(group_id),
                    sender_id=user_id,
                    sender_name=sender_name,
                    url=str(data.get("url") or "").strip(),
                    file_id=str(data.get("file_id") or "").strip(),
                    busid=str(data.get("busid") or "").strip(),
                    size=_int(data.get("file_size") or data.get("size")),
                    ts=stamp,
                    msg_id=f"onebot:{group_id}:{message_id}" if message_id else "",
                    hint=hint,
                )
            )
        elif kind == "image":
            if _is_sticker(segment):
                continue
            size = _int(data.get("file_size") or data.get("size"))
            if 0 < size < MIN_IMAGE_BYTES:
                continue
            url = str(data.get("url") or "").strip()
            file_id = str(data.get("file") or data.get("file_id") or "").strip()
            if not url and not file_id:
                continue
            found.append(
                Attachment(
                    kind="image",
                    name=str(data.get("file") or data.get("filename") or "image.jpg").strip(),
                    group_id=group_id,
                    group_name=settings.group_name(group_id),
                    sender_id=user_id,
                    sender_name=sender_name,
                    url=url,
                    file_id=file_id,
                    size=size,
                    ts=stamp,
                    msg_id=f"onebot:{group_id}:{message_id}" if message_id else "",
                    hint=hint,
                )
            )
    return found


def attachment_from_notice(payload: Any, settings: Settings) -> Attachment | None:
    """群文件上传通知（group_upload）。"""
    if not isinstance(payload, dict):
        return None
    if str(payload.get("post_type") or "") != "notice":
        return None
    if str(payload.get("notice_type") or "") != "group_upload":
        return None

    group_id = str(payload.get("group_id") or "")
    if not group_id or not settings.accepts_group(group_id):
        return None
    user_id = str(payload.get("user_id") or "")
    info = payload.get("file") or {}
    if not isinstance(info, dict):
        return None
    name = str(info.get("name") or info.get("file") or "").strip()
    if not name:
        return None
    file_id = str(info.get("id") or info.get("file_id") or "").strip()
    return Attachment(
        kind="file",
        name=name,
        group_id=group_id,
        group_name=settings.group_name(group_id),
        sender_id=user_id,
        sender_name=user_id or "群成员",
        file_id=file_id,
        busid=str(info.get("busid") or "").strip(),
        size=_int(info.get("size") or info.get("file_size")),
        ts=iso(payload.get("time")) or iso(now_local()),
        msg_id=f"onebot:{group_id}:upload:{file_id or name}",
    )


def attachments_from_history(message: dict[str, Any], settings: Settings) -> list[Attachment]:
    """补采历史消息时顺带找回漏掉的附件。"""
    if not isinstance(message, dict):
        return []
    payload = dict(message)
    payload.setdefault("post_type", "message")
    payload.setdefault("message_type", "group")
    return attachments_from_message(payload, settings)


# --------------------------------------------------------------------- 文本
def _strip_xml_runs(node: ET.Element) -> str:
    """把 docx/pptx 的 XML 文本节点拼成纯文本。"""
    chunks: list[str] = []
    for parent in node.iter():
        tag = str(parent.tag).rsplit("}", 1)[-1]
        if tag in {"t", "instrText"}:
            if parent.text:
                chunks.append(parent.text)
        elif tag in {"br", "tab"}:
            chunks.append(" ")
        elif tag in {"p", "tr"}:
            chunks.append("\n")
    text = "".join(chunks)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _read_zip_member(archive: zipfile.ZipFile, name: str) -> bytes:
    with archive.open(name) as handle:
        return handle.read()


def extract_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        names = [item for item in archive.namelist() if item.startswith("word/") and item.endswith(".xml")]
        parts: list[str] = []
        for name in sorted(names):
            if "document" not in name and "header" not in name and "footer" not in name:
                continue
            try:
                parts.append(_strip_xml_runs(ET.fromstring(_read_zip_member(archive, name))))
            except (ET.ParseError, KeyError, OSError):
                continue
    return "\n".join(part for part in parts if part)


def extract_pptx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        slides = sorted(
            (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
            key=lambda item: _int(re.search(r"(\d+)", item).group(1)) if re.search(r"(\d+)", item) else 0,
        )
        parts: list[str] = []
        for name in slides:
            try:
                text = _strip_xml_runs(ET.fromstring(_read_zip_member(archive, name)))
            except (ET.ParseError, KeyError, OSError):
                continue
            if text:
                parts.append(f"【第{len(parts) + 1}页】{text}")
    return "\n".join(parts)


def extract_pdf(path: Path) -> str:
    try:
        import pypdf  # type: ignore
    except ImportError:  # pragma: no cover - 依赖缺失时降级
        return ""
    try:
        reader = pypdf.PdfReader(str(path))
    except Exception as error:  # noqa: BLE001 - 损坏/加密 PDF
        LOGGER.info("PDF 解析失败 %s：%s", path.name, error)
        return ""
    parts: list[str] = []
    for index, page in enumerate(reader.pages, start=1):
        if index > MAX_PDF_PAGES:
            parts.append("……（文档较长，仅读前 %d 页）" % MAX_PDF_PAGES)
            break
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - 单页失败不影响整体
            continue
        text = re.sub(r"[ \t]+", " ", text).strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def extract_xlsx(path: Path) -> str:
    try:
        import openpyxl  # type: ignore
    except ImportError:  # pragma: no cover
        return ""
    try:
        book = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as error:  # noqa: BLE001
        LOGGER.info("Excel 解析失败 %s：%s", path.name, error)
        return ""
    parts: list[str] = []
    try:
        for sheet in book.worksheets:
            rows: list[str] = []
            for row_index, row in enumerate(sheet.iter_rows(values_only=True)):
                if row_index >= 200:
                    rows.append("……（仅读前 200 行）")
                    break
                cells = [str(cell).strip() for cell in row if cell is not None and str(cell).strip()]
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                parts.append(f"【工作表：{sheet.title}】\n" + "\n".join(rows))
    finally:
        try:
            book.close()
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(parts)


def decode_text_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030", "utf-16", "big5"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def extract_zip(path: Path, depth: int = 0) -> str:
    """只解压白名单文档，限制条目数、总量和嵌套层数。"""
    if depth > 1:
        return ""
    parts: list[str] = []
    with zipfile.ZipFile(path) as archive:
        entries = [item for item in archive.infolist() if not item.is_dir()]
        for entry in entries[:MAX_ZIP_ENTRIES]:
            name = entry.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                continue
            suffix = Path(name).suffix.lower()
            if suffix not in (TEXT_EXTS | DOC_EXTS) or suffix in ARCHIVE_EXTS:
                continue
            if entry.file_size > MAX_ZIP_TOTAL_BYTES:
                continue
            try:
                payload = _read_zip_member(archive, entry.filename)
            except (KeyError, OSError, zipfile.BadZipFile):
                continue
            if len(payload) > MAX_ZIP_TOTAL_BYTES:
                continue
            inner = extract_bytes(payload, suffix)
            if inner:
                parts.append(f"【压缩包内文件：{Path(name).name}】\n{inner}")
            if sum(len(part) for part in parts) > DOC_CHUNK_CHARS * DOC_MAX_CHUNKS:
                break
        if len(entries) > MAX_ZIP_ENTRIES:
            parts.append(f"……（压缩包共 {len(entries)} 个文件，仅读前 {MAX_ZIP_ENTRIES} 个）")
    return "\n\n".join(parts)


def extract_bytes(data: bytes, suffix: str) -> str:
    """对内存里的字节做解析（zip 内部复用）。"""
    import tempfile

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
        handle.write(data)
        temp_path = Path(handle.name)
    try:
        return extract_text(temp_path, suffix)
    finally:
        temp_path.unlink(missing_ok=True)


def extract_text(path: Path, name: str = "") -> str:
    """按扩展名抽取文本，失败返回空字符串。"""
    suffix = (Path(name or path.name).suffix or path.suffix).lower()
    try:
        if suffix in TEXT_EXTS:
            return decode_text_bytes(path.read_bytes())
        if suffix == ".docx":
            return extract_docx(path)
        if suffix == ".pptx":
            return extract_pptx(path)
        if suffix == ".pdf":
            return extract_pdf(path)
        if suffix in {".xlsx", ".xlsm"}:
            return extract_xlsx(path)
        if suffix == ".zip":
            return extract_zip(path)
    except (zipfile.BadZipFile, OSError, ValueError) as error:
        LOGGER.info("附件解析失败 %s：%s", path.name, error)
        return ""
    return ""


# ------------------------------------------------------------------- 下载
def sanitize_name(name: str, fallback: str = "attachment") -> str:
    value = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", str(name or "").strip())
    value = value.strip(". ") or fallback
    if len(value) > 60:
        stem, dot, ext = value.rpartition(".")
        value = (stem[:55] + dot + ext[:4]) if dot else value[:60]
    return value


# --------------------------------------------------------------- 下载安全
# 附件 URL 来自外部（QQ 群文件/图片的 OneBot 事件），必须按不可信输入处理：
# 1) 只允许 http/https；
# 2) 连接前解析 DNS 并检查所有 IP，拒绝本机/内网/链路本地/保留/云元数据地址；
# 3) 校验通过后直连已校验的 IP（Host 头与 TLS SNI 仍是原域名），
#    因此校验后 DNS 再被改成本机地址也不会生效（DNS Rebinding 防护）。
ALLOWED_DOWNLOAD_SCHEMES = ("http", "https")
MAX_DOWNLOAD_REDIRECTS = 5
_BLOCKED_HOSTNAMES = {"localhost"}
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(item)
    for item in (
        # IPv4：本网络、私网、CGNAT、回环、链路本地（含云元数据 169.254.169.254）、
        # 保留/基准测试网段、组播、保留段
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "240.0.0.0/4",
        # IPv6：未指定、回环、NAT64、ULA、链路本地、组播、文档用段
        "::/128",
        "::1/128",
        "64:ff9b::/96",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
        "2001:db8::/32",
    )
)
Resolver = Callable[[str, int], list[str]]


@dataclass(frozen=True)
class _DownloadTarget:
    url: str
    scheme: str
    host: str
    port: int
    host_header: str
    path: str
    ip: str


def _blocked_ip(ip_text: str) -> bool:
    """判断单个 IP 是否属于本机/内网/保留地址；只接受 IP 字面量。"""
    try:
        address = ipaddress.ip_address(ip_text)
    except ValueError:
        return True
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return _blocked_ip(str(address.ipv4_mapped))
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return True
    return any(address in network for network in _BLOCKED_NETWORKS)


def _resolve_ips(host: str, port: int, resolver: Resolver | None = None) -> list[str]:
    """解析主机名到 IP 列表；resolver 仅供测试注入。"""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return [host]
    try:
        if resolver is not None:
            return list(resolver(host, port) or [])
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError, ValueError) as error:
        raise AttachmentError(f"无法解析下载地址：{error}") from error
    ips: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in ips:
            ips.append(address)
    return ips


def _validate_download_url(url: str, *, resolver: Resolver | None = None) -> _DownloadTarget:
    """校验下载 URL：只允许 http(s)，且拒绝解析到本机/内网/元数据地址的目标。"""
    parts = urllib.parse.urlsplit(str(url or "").strip())
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_DOWNLOAD_SCHEMES:
        raise AttachmentError(f"不支持的下载协议：{parts.scheme or '(空)'}")
    if parts.username or parts.password:
        raise AttachmentError("下载地址不允许包含用户名/密码")
    host = parts.hostname or ""
    if not host:
        raise AttachmentError("下载地址缺少主机名")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as error:
        raise AttachmentError(f"下载地址端口无效：{error}") from error
    lowered = host.lower().rstrip(".")
    if lowered in _BLOCKED_HOSTNAMES or lowered.endswith(".localhost"):
        raise AttachmentError(f"拒绝访问本机地址：{host}")
    ips = _resolve_ips(host, port, resolver)
    if not ips:
        raise AttachmentError(f"下载地址未解析出 IP：{host}")
    for ip in ips:
        if _blocked_ip(ip):
            raise AttachmentError(f"拒绝访问本机/内网地址：{host} -> {ip}")
    netloc = parts.netloc.rpartition("@")[2]  # 已拒绝 userinfo，这里只剩 host[:port]
    path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
    return _DownloadTarget(
        url=url,
        scheme=scheme,
        host=host,
        port=port,
        host_header=netloc or host,
        path=path,
        ip=ips[0],
    )


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP 连接：直接连到已校验 IP，Host 头仍指向原域名。"""


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS 连接：直接连到已校验 IP，TLS 校验与 SNI 仍使用原域名。"""

    def __init__(
        self, ip: str, port: int, server_name: str, context: ssl.SSLContext, timeout: int
    ) -> None:
        super().__init__(ip, port, timeout=timeout, context=context)
        self._server_name = server_name

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), self.timeout, self.source_address)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self._server_name)
        except Exception:
            sock.close()
            raise


class _PinnedResponse:
    """把 http.client 的响应与连接包成一个可关闭对象。"""

    def __init__(self, connection: http.client.HTTPConnection, response: http.client.HTTPResponse):
        self._connection = connection
        self._response = response

    @property
    def status(self) -> int:
        return int(self._response.status)

    def getheader(self, name: str) -> str | None:
        return self._response.getheader(name)

    def read(self, size: int = -1) -> bytes:
        return self._response.read(size)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()


def _open_target(target: _DownloadTarget, timeout: int) -> _PinnedResponse:
    if target.scheme == "https":
        connection: http.client.HTTPConnection = _PinnedHTTPSConnection(
            target.ip, target.port, target.host, ssl.create_default_context(), timeout
        )
    else:
        connection = _PinnedHTTPConnection(target.ip, target.port, timeout=timeout)
    try:
        connection.request(
            "GET",
            target.path,
            headers={
                "User-Agent": USER_AGENT,
                "Host": target.host_header,
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
    except Exception:
        connection.close()
        raise
    return _PinnedResponse(connection, response)


def download(
    url: str,
    dest: Path,
    max_bytes: int,
    timeout: int = 30,
    *,
    _resolver: Resolver | None = None,
    _transport: Callable[[_DownloadTarget, int], Any] | None = None,
) -> Path:
    """下载附件：只允许 http(s)，拒绝本机/内网目标，重定向逐跳重新校验。

    _resolver / _transport 仅供测试注入，生产调用必须使用默认值。
    """
    transport = _transport or _open_target
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(dest.suffix + ".part")
    total = 0
    response: Any = None
    try:
        current = url
        for _ in range(MAX_DOWNLOAD_REDIRECTS + 1):
            target = _validate_download_url(current, resolver=_resolver)
            response = transport(target, timeout)
            status = int(getattr(response, "status", 0) or 0)
            if status in (301, 302, 303, 307, 308):
                location = str(response.getheader("Location") or "").strip()
                response.close()
                response = None
                if not location:
                    raise AttachmentError("重定向缺少 Location")
                current = urllib.parse.urljoin(current, location)
                continue
            if status < 200 or status >= 300:
                response.close()
                response = None
                raise AttachmentError(f"下载失败：HTTP {status}")
            break
        else:
            raise AttachmentError("重定向次数过多")
        length = int(response.getheader("Content-Length") or 0)
        if length and length > max_bytes:
            raise AttachmentError(f"文件超过上限（{length} 字节）")
        with temp.open("wb") as handle:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise AttachmentError("文件超过下载上限")
                handle.write(chunk)
        if length and total != length:
            raise AttachmentError(f"下载不完整：收到 {total}/{length} 字节")
    except AttachmentError:
        temp.unlink(missing_ok=True)
        raise
    except (OSError, TimeoutError, ValueError, http.client.HTTPException) as error:
        temp.unlink(missing_ok=True)
        raise AttachmentError(f"下载失败：{error}") from error
    finally:
        if response is not None:
            response.close()
    if total == 0:
        temp.unlink(missing_ok=True)
        raise AttachmentError("下载内容为空")
    temp.replace(dest)
    return dest


# ------------------------------------------------------------------ AI 调用
def chat_completion(
    settings: Settings,
    messages: list[dict[str, Any]],
    *,
    model: str = "",
    timeout: int = 60,
    max_tokens: int = 600,
) -> str:
    """文本 / 视觉统一入口。

    传输与重试交给 Provider（`qq_live_digest.providers`），本模块不再自己拼 HTTP 请求，
    因此换模型供应商只改配置，不动这里的业务代码（Roadmap A4）。
    """
    provider = providers.build_provider(settings, model=model or settings.dashscope_model)
    try:
        result = providers.complete_with_retries(
            provider,
            messages,
            retries=settings.llm_max_retries,
            backoff=settings.llm_retry_backoff,
            label="视觉/文档模型",
            temperature=0.1,
            max_tokens=max_tokens,
            timeout=max(15, int(timeout)),
        )
    except LLMError as error:
        raise AttachmentError(str(error)) from error
    return result.text


def _parse_document_json(raw: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", str(raw or ""), re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _format_document_extract(data: dict[str, Any], name: str) -> str:
    lines: list[str] = []
    summary = str(data.get("summary") or "").strip()
    audience = str(data.get("audience") or "").strip()
    condition = str(data.get("condition") or "").strip()
    action = str(data.get("action") or "").strip()
    deadline = str(data.get("deadline") or "").strip()
    details = data.get("details")
    if summary:
        lines.append(summary)
    if audience:
        lines.append(f"适用对象：{audience}")
    if condition:
        lines.append(f"适用条件：{condition}")
    if isinstance(details, list):
        for value in details:
            text = str(value or "").strip()
            if text:
                lines.append(f"- {text}")
    elif isinstance(details, str) and details.strip():
        lines.append(f"- {details.strip()}")
    if action:
        lines.append(f"需要行动：{action}")
    if deadline:
        lines.append(f"截止时间：{deadline}")
    return "\n".join(lines).strip()


def summarize_document(text: str, settings: Settings, name: str) -> str:
    """从完整文档正文提取结构化通知，不再先压成120字。"""
    body = text.strip()
    if not body:
        return ""
    raw = chat_completion(
        settings,
        [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": DOC_PROMPT.format(name=name, body=body)},
        ],
        timeout=settings.llm_timeout,
        max_tokens=2400,
    )
    data = _parse_document_json(raw)
    if not data:
        return raw.strip()
    return _format_document_extract(data, name) or raw.strip()


def image_text(path: Path, settings: Settings) -> str:
    """用视觉模型提取图片文字。"""
    raw = path.read_bytes()
    if not raw:
        return ""
    suffix = path.suffix.lower().lstrip(".") or "jpeg"
    if suffix == "jpg":
        suffix = "jpeg"
    encoded = base64.b64encode(raw).decode("ascii")
    return chat_completion(
        settings,
        [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": IMAGE_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/{suffix};base64,{encoded}"},
                    },
                ],
            },
        ],
        model=settings.vision_model,
        timeout=settings.http_timeout * 5,
        max_tokens=2000,
    )


def image_bytes_text(raw: bytes, suffix: str, settings: Settings) -> str:
    """用视觉模型提取内存图片文字，供扫描版 PDF 的页面复用。"""
    if not raw:
        return ""
    suffix = str(suffix or "jpeg").lower().lstrip(".") or "jpeg"
    if suffix == "jpg":
        suffix = "jpeg"
    encoded = base64.b64encode(raw).decode("ascii")
    image_url = "data:" + "image/" + suffix + ";base64," + encoded
    return chat_completion(
        settings,
        [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": IMAGE_PROMPT},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
        model=settings.vision_model,
        timeout=settings.http_timeout * 5,
        max_tokens=2000,
    )


def pdf_ocr_text(path: Path, settings: Settings) -> str:
    """把没有文本层的 PDF 逐页渲染成图片，再用视觉模型 OCR。"""
    if not settings.vision_enabled:
        return ""
    try:
        import pypdfium2 as pdfium  # type: ignore
        from PIL import Image  # type: ignore  # noqa: F401 - 仅确认依赖可用
    except ImportError as error:
        raise AttachmentError("未安装 pypdfium2/Pillow，无法 OCR 扫描版 PDF") from error
    parts: list[str] = []
    document = None
    try:
        document = pdfium.PdfDocument(str(path))
        page_count = min(len(document), max(1, int(settings.pdf_ocr_max_pages)))
        for index in range(page_count):
            page = document[index]
            bitmap = page.render(scale=2)  # 2x 缩放，与原 Matrix(2, 2) 一致
            try:
                buffer = io.BytesIO()
                bitmap.to_pil().convert("RGB").save(buffer, format="PNG")
            finally:
                bitmap.close()
                page.close()
            text = image_bytes_text(buffer.getvalue(), "png", settings)
            if is_meaningful_text(text):
                parts.append(f"【第{index + 1}页】\n{text}")
    except Exception as error:
        raise AttachmentError(f"扫描 PDF OCR 失败：{error}") from error
    finally:
        if document is not None:
            document.close()
    return "\n\n".join(parts)


def is_meaningful_text(text: str) -> bool:
    value = re.sub(r"\s+", "", str(text or ""))
    if len(value) < 12:
        return False
    return not any(marker in value for marker in NO_TEXT_MARKERS)


# --------------------------------------------------------------- 后台处理线程
@dataclass
class AttachmentStats:
    queued: int = 0
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    last_error: str = ""
    last_at: str = ""
    counters: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = {
            "queued": self.queued,
            "processed": self.processed,
            "skipped": self.skipped,
            "failed": self.failed,
            "last_error": self.last_error,
            "last_at": self.last_at,
        }
        data.update(self.counters)
        return data


class AttachmentWorker:
    """单线程后台队列：下载 → 解析 → 摘要 → 交给 on_record。"""

    def __init__(
        self,
        settings: Settings,
        on_record: Callable[[dict[str, Any]], Any],
        *,
        logger: logging.Logger | None = None,
        napcat_call: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.settings = settings
        self.on_record = on_record
        self.logger = logger or LOGGER
        self.napcat_call = napcat_call
        self.queue: queue.Queue[Attachment] = queue.Queue()
        self.stats = AttachmentStats()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._recent: deque[float] = deque(maxlen=256)
        self._lock = threading.Lock()
        self._warned_at = 0.0

    # ------------------------------------------------------------ 生命周期
    @property
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.is_alive:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="attachments", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def submit(self, attachment: Attachment) -> bool:
        if not self.settings.attachments_enabled or attachment is None:
            return False
        with self._lock:
            if attachment.key in self._seen:
                return False
            self._seen[attachment.key] = time.time()
            while len(self._seen) > 500:
                self._seen.popitem(last=False)
        self.queue.put(attachment)
        self.stats.queued += 1
        self.logger.info(
            "收到群附件 group=%s kind=%s name=%s size=%s sender=%s",
            attachment.group_name or attachment.group_id,
            attachment.kind,
            attachment.name,
            attachment.size_text or "?",
            attachment.sender_name or attachment.sender_id or "?",
        )
        return True

    # -------------------------------------------------------------- 主循环
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                attachment = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self.process(attachment)
            except Exception as error:  # noqa: BLE001 - 单条失败不能拖垮线程
                self.stats.failed += 1
                self.stats.last_error = str(error)[:200]
                self.logger.exception("附件处理失败：%s", attachment.name)
            finally:
                self.stats.last_at = iso(now_local())
                self.queue.task_done()

    def _rate_limited(self) -> bool:
        limit = max(1, int(self.settings.attachment_max_per_hour))
        now = time.time()
        with self._lock:
            while self._recent and now - self._recent[0] > 3600:
                self._recent.popleft()
            if len(self._recent) >= limit:
                if now - self._warned_at > 1800:
                    self._warned_at = now
                    self.logger.warning("附件处理已达每小时上限 %d，暂停解析新附件。", limit)
                return True
            self._recent.append(now)
        return False

    # ------------------------------------------------------------ 单条处理
    def process(self, attachment: Attachment) -> dict[str, Any] | None:
        if self._rate_limited():
            self.stats.skipped += 1
            return None

        max_bytes = max(1, int(self.settings.attachment_max_mb)) * 1024 * 1024
        if attachment.kind == "file" and attachment.size and attachment.size > max_bytes:
            self.stats.skipped += 1
            self.logger.info("附件超过 %dMB，仅记录名字：%s", self.settings.attachment_max_mb, attachment.name)
            return self._emit(attachment, body="", reason="too_large")

        path = self._fetch(attachment, max_bytes)
        if path is None:
            self.stats.skipped += 1
            return None
        try:
            return self.process_local(attachment, path)
        finally:
            if attachment.kind == "image":
                self._discard(path)
        return self.process_local(attachment, path)

    def process_local(self, attachment: Attachment, path: Path) -> dict[str, Any] | None:
        """解析已经在本地的附件，生成待入库的消息记录。"""

        if attachment.kind == "image":
            text = image_text(path, self.settings)
            if not is_meaningful_text(text):
                self.stats.skipped += 1
                self.logger.info("图片没有可读通知内容，已忽略：%s", attachment.name)
                return None
            body = f"【图片】\n{text}"
            return self._emit(attachment, body=body, source_text=text)

        text = extract_text(path, attachment.name)
        plain_text = re.sub(r"\s+", "", text)
        if attachment.suffix == ".pdf" and len(plain_text) < MIN_DOC_TEXT_CHARS:
            try:
                ocr_text = pdf_ocr_text(path, self.settings)
            except AttachmentError as error:
                self.logger.info("扫描 PDF OCR 失败，回退文本层：%s（%s）", attachment.name, error)
            else:
                if ocr_text.strip():
                    text = ocr_text
                    self.logger.info("扫描 PDF OCR 完成：%s", attachment.name)
        if not text.strip():
            self.stats.skipped += 1
            self.logger.info("附件没有可提取文本（可能是扫描件或缺依赖）：%s", attachment.name)
            return self._emit(attachment, body="", reason="no_text")

        source_text = text[: max(1000, int(self.settings.document_max_chars))]
        if len(text) > len(source_text):
            source_text += "\n\n……（文件较长，仅保留并分析前 %d 字）" % len(source_text)
        try:
            summary = summarize_document(source_text, self.settings, attachment.name)
        except AttachmentError as error:
            self.logger.info("附件结构化提取失败，改用正文截断：%s（%s）", attachment.name, error)
            summary = source_text[:400]
        body = f"【文件】{attachment.name}\n{summary}".strip()
        return self._emit(attachment, body=body, source_text=source_text)

    def _emit(
        self,
        attachment: Attachment,
        *,
        body: str,
        reason: str = "",
        source_text: str = "",
    ) -> dict[str, Any] | None:
        size = attachment.size_text
        if not body:
            note = "超过大小上限，未解析" if reason == "too_large" else "未能解析出文字"
            body = f"【文件】{attachment.name}（{size or '大小未知'}，{note}）"
        content = body[:MAX_RECORD_CHARS]
        record = {
            "msg_id": f"onebot:{attachment.group_id}:attach:{attachment.key}",
            "source": "onebot-attachment",
            "event": "GROUP_ATTACHMENT",
            "group_id": attachment.group_id,
            "group_name": attachment.group_name,
            "sender_id": attachment.sender_id,
            "sender_name": attachment.sender_name,
            "ts": attachment.ts or iso(now_local()),
            "received_at": iso(now_local()),
            "content": content,
            "source_text": source_text or "",
        }
        self.stats.processed += 1
        self.stats.counters[f"processed_{attachment.kind}"] = (
            self.stats.counters.get(f"processed_{attachment.kind}", 0) + 1
        )
        self.logger.info(
            "附件解析完成 group=%s kind=%s name=%s 摘要=%s",
            attachment.group_name or attachment.group_id,
            attachment.kind,
            attachment.name,
            content.replace("\n", " ")[:80],
        )
        try:
            self.on_record(record)
        except Exception:  # noqa: BLE001
            self.logger.exception("附件记录入库失败：%s", attachment.name)
        return record

    def _discard(self, path: Path) -> None:
        """图片读完即删，不在本地留下原始截图。"""
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            self.logger.warning("图片临时文件删除失败 %s：%s", path.name, error)

    def _fetch(self, attachment: Attachment, max_bytes: int) -> Path | None:
        target_dir = self.settings.data_dir / "files"
        dest = target_dir / f"{attachment.key}_{sanitize_name(attachment.name)}"
        if dest.is_file() and dest.stat().st_size > 0:
            return dest
        url = attachment.url
        if not url and self.napcat_call and attachment.file_id:
            payload: dict[str, Any] = {"group_id": attachment.group_id, "file_id": attachment.file_id}
            if attachment.busid:
                try:
                    payload["busid"] = int(attachment.busid)
                except ValueError:
                    payload["busid"] = attachment.busid
            try:
                result = self.napcat_call("get_group_file_url", payload)
                url = str((result.get("data") or {}).get("url") or "").strip()
            except Exception as error:  # noqa: BLE001
                self.logger.warning("取附件下载地址失败 %s：%s", attachment.name, error)
        if not url:
            self.logger.info("附件没有可下载地址，跳过：%s", attachment.name)
            return None
        try:
            return download(url, dest, max_bytes, timeout=max(20, self.settings.http_timeout * 2))
        except AttachmentError as error:
            self.logger.warning("附件下载失败 %s：%s", attachment.name, error)
            return None

    def describe(self) -> dict[str, Any]:
        return self.stats.as_dict()


def cleanup_files(directory: Path, *, days: int) -> int:
    """清理过期的附件缓存。"""
    if days <= 0 or not directory.is_dir():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for item in directory.iterdir():
        try:
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink()
                removed += 1
        except OSError:
            continue
    return removed
