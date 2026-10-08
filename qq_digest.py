#!/usr/bin/env python3
"""Turn exported QQ group messages into a compact notice digest."""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import email
import html
import json
import re
import sys
import zipfile
from dataclasses import dataclass
from email import policy
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from qq_live_digest import providers

SUPPORTED_EXTS = {".json", ".txt", ".md", ".html", ".htm", ".mht", ".mhtml", ".zip"}

FULL_TS_RE = re.compile(
    r"(?P<year>20\d{2})[-/.年](?P<month>\d{1,2})[-/.月](?P<day>\d{1,2})日?"
    r"(?:[ T]+(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?"
)
SHORT_TS_RE = re.compile(
    r"(?P<month>\d{1,2})[-/.月](?P<day>\d{1,2})日?"
    r"(?:[ T]+(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?"
)
DATE_ANY_RE = re.compile(
    r"(?:20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?"
    r"|\d{1,2}[-/.月]\d{1,2}日?)"
    r"(?:[ T]+\d{1,2}:\d{2}(?::\d{2})?)?"
)
URL_RE = re.compile(r"https?://[^\s<>\"'）)\]】]+", re.IGNORECASE)
CLOCK_RE = re.compile(
    r"(?<!\d)(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上|夜里|晚)?"
    r"(?P<hour>\d{1,2}|[零〇一二两三四五六七八九十]{1,3})"
    r"\s*(?P<separator>[:：点时])\s*"
    r"(?P<minute>\d{1,2}|[零〇一二两三四五六七八九十]{1,3}|半)?(?:分)?"
)

CHATTER_RE = re.compile(
    r"^(?:收到|好的|好滴|好嘞|谢谢|感谢|辛苦了|哈哈哈+|笑死|\+1|"
    r"在吗|有人吗|打卡|签到|早上好|晚安|晚安啦|表情|撤回了一条消息)[！!。.~～]*$"
)

URGENT_WORDS = (
    "紧急", "尽快", "务必", "马上", "立即", "速", "最后通知", "逾期",
    "过期", "今天", "今日", "今晚", "明天", "明日", "后天", "截止",
)
ACTION_WORDS = (
    "提交", "填写", "报名", "申请", "申报", "缴费", "交费", "领取",
    "签到", "确认", "核对", "回复", "接龙", "统计", "上报", "报送",
    "选课", "预约", "下载", "安装", "完成", "参加", "观看", "交表",
    "填表", "投票", "测评", "问卷", "材料", "办理", "注册", "认证",
)
ACADEMIC_WORDS = (
    "考试", "补考", "缓考", "期中", "期末", "成绩", "教务", "课程",
    "调课", "停课", "上课", "教室", "学分", "学籍", "培养方案", "论文",
    "答辩", "导师", "四六级", "普通话", "竞赛", "选课", "实验", "作业",
)
ADMIN_WORDS = (
    "学院", "学校", "辅导员", "老师", "班长", "学委", "团支书", "通知",
    "会议", "讲座", "活动", "志愿", "评奖", "评优", "奖学金", "助学金",
    "资助", "就业", "实习", "招聘", "体检", "医保", "宿舍", "校园卡",
    "安全", "消防", "实验室", "图书馆", "学费", "住宿", "青年大学习",
    "党课", "团课",
)
AUTHORITY_WORDS = (
    "辅导员", "老师", "班主任", "班长", "学委", "团支书", "教务",
    "学院", "办公室", "管理员", "负责人",
)

DIRECTIVE_WORDS = (
    "请", "务必", "必须", "要求", "尽快", "按时", "记得",
    "不要忘记", "截止",
)

TEXT_KEYS = (
    "content", "text", "plainText", "message", "msg", "summary",
    "richText", "html", "elements",
)
SENDER_KEYS = (
    "senderName", "sender_name", "sender", "nickname", "nickName", "name",
    "fromName", "from", "userName", "memberName", "displayName",
)
TIME_KEYS = (
    "timestamp", "time", "msgTime", "sendTime", "send_time", "date",
    "datetime", "createTime", "createdAt",
)
GROUP_NAME_KEYS = (
    "groupName", "group_name", "peerName", "peer_name", "chatName",
    "conversationName", "sessionName",
)


@dataclass
class Message:
    timestamp: dt.datetime | None
    sender: str
    text: str
    source: str = ""


class HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self.skip_depth += 1
        elif tag.lower() in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self._newline()

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self.skip_depth:
            self.skip_depth -= 1
        elif tag.lower() in {"p", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self._newline()

    def handle_data(self, data: str) -> None:
        if not self.skip_depth and data:
            self.parts.append(data)

    def _newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def get_text(self) -> str:
        return clean_text("".join(self.parts))


def clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_for_match(text: str) -> str:
    return re.sub(r"[\W_]+", "", clean_text(text).lower(), flags=re.UNICODE)


def decode_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030", "utf-16", "big5"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_timestamp(value: Any) -> dt.datetime | None:
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return None
        if number > 10_000_000_000:
            number /= 1000
        if number >= 1_000_000_000:
            try:
                return dt.datetime.fromtimestamp(number).replace(tzinfo=None)
            except (OverflowError, OSError, ValueError):
                return None
        return None

    text = clean_text(value)
    if not text:
        return None

    if re.fullmatch(r"\d{10,13}", text):
        return parse_timestamp(int(text))

    match = FULL_TS_RE.search(text) or SHORT_TS_RE.search(text)
    if not match:
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo:
                parsed = parsed.astimezone().replace(tzinfo=None)
            return parsed
        except ValueError:
            return None

    groups = match.groupdict()
    now = dt.datetime.now()
    try:
        year = int(groups.get("year") or now.year)
        month = int(groups["month"])
        day = int(groups["day"])
        hour = int(groups.get("hour") or 0)
        minute = int(groups.get("minute") or 0)
        second = int(groups.get("second") or 0)
        return dt.datetime(year, month, day, hour, minute, second)
    except (TypeError, ValueError):
        return None


def _value_to_text(value: Any, depth: int = 0) -> str:
    if depth > 5 or value is None:
        return ""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return clean_text("\n".join(filter(None, (_value_to_text(item, depth + 1) for item in value))))
    if not isinstance(value, dict):
        return ""

    kind = str(value.get("type") or value.get("kind") or "").lower()
    if kind in {"image", "img", "face", "emoji", "market_face"}:
        return "[图片]"
    if kind in {"file", "video", "audio", "record", "voice"}:
        name = value.get("fileName") or value.get("name") or value.get("file") or ""
        return f"[文件: {clean_text(name)}]" if name else f"[{kind}]"

    parts: list[str] = []
    for key in (
        "text", "content", "value", "summary", "title", "fileName", "url",
    ):
        if key in value:
            part = _value_to_text(value[key], depth + 1)
            if part and part not in parts:
                parts.append(part)
    return clean_text("\n".join(parts))


def extract_text_from_object(obj: dict[str, Any]) -> str:
    for key in TEXT_KEYS:
        if key in obj:
            value = _value_to_text(obj[key])
            if value:
                return value
    return ""


def find_sender(obj: dict[str, Any], depth: int = 0) -> str:
    if depth > 3:
        return ""
    for key in SENDER_KEYS:
        if key not in obj:
            continue
        value = obj[key]
        if isinstance(value, str):
            sender = clean_text(value)
            if sender and len(sender) <= 80:
                return sender
        elif isinstance(value, dict):
            sender = find_sender(value, depth + 1)
            if sender:
                return sender
    return ""


def find_timestamp(obj: dict[str, Any]) -> dt.datetime | None:
    for key in TIME_KEYS:
        if key in obj:
            parsed = parse_timestamp(obj[key])
            if parsed:
                return parsed
    for value in obj.values():
        if isinstance(value, dict):
            parsed = find_timestamp(value)
            if parsed:
                return parsed
    return None


def find_group_name(node: Any, depth: int = 0) -> str:
    if depth > 2:
        return ""
    if isinstance(node, list):
        for item in node[:3]:
            found = find_group_name(item, depth + 1)
            if found:
                return found
        return ""
    if not isinstance(node, dict):
        return ""
    for key in GROUP_NAME_KEYS:
        value = node.get(key)
        if isinstance(value, str):
            value = clean_text(value)
            if 1 <= len(value) <= 100:
                return value
    for key in ("group", "peer", "session", "conversation", "chatInfo", "metadata"):
        value = node.get(key)
        if isinstance(value, (dict, list)):
            found = find_group_name(value, depth + 1)
            if found:
                return found
    return ""


def looks_like_message(obj: dict[str, Any], hint: str = "") -> bool:
    text = extract_text_from_object(obj)
    if len(text) < 2:
        return False
    timestamp = find_timestamp(obj)
    sender = find_sender(obj)
    keys = {key.lower() for key in obj}
    message_hint = any(
        token in hint.lower() for token in ("message", "record", "msg", "history", "chat")
    )
    explicit_shape = bool(keys & {"elements", "msgelements", "sendtime", "msgtime"})
    return bool(timestamp or sender or message_hint or explicit_shape)


def walk_json(node: Any, source: str, hint: str = "") -> list[Message]:
    messages: list[Message] = []
    if isinstance(node, list):
        for index, item in enumerate(node):
            messages.extend(walk_json(item, source, f"{hint}[{index}]"))
        return messages
    if not isinstance(node, dict):
        return messages

    if looks_like_message(node, hint):
        text = extract_text_from_object(node)
        if text:
            messages.append(
                Message(
                    timestamp=find_timestamp(node),
                    sender=find_sender(node) or "未知",
                    text=text,
                    source=source,
                )
            )

    for key, value in node.items():
        if not isinstance(value, (list, dict)):
            continue
        if key.lower() in {"content", "text", "elements", "sender", "from", "message"}:
            continue
        messages.extend(walk_json(value, source, f"{hint}.{key}"))
    return messages


def _parse_inline_sender(before: str, after: str) -> tuple[str, str]:
    before = clean_text(before).strip("[]【】()（） ")
    after = clean_text(after).strip("[]【】()（） ")
    if not after:
        return before[:60], ""

    match = re.match(r"^(?P<sender>[^:：\n]{1,40})\s*[:：]\s*(?P<body>.*)$", after, re.S)
    if match:
        return clean_text(match.group("sender")), clean_text(match.group("body"))

    if before:
        return before[:60], after

    chunks = after.split(maxsplit=1)
    if len(chunks) == 2 and 1 <= len(chunks[0]) <= 24:
        return chunks[0].strip(), chunks[1].strip()
    return "未知", after


def _fallback_text_messages(text: str, source: str) -> list[Message]:
    messages: list[Message] = []
    for block in re.split(r"\n\s*\n", text):
        block = clean_text(block)
        if not block:
            continue
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        sender = "未知"
        body_lines = lines
        if lines:
            match = re.match(r"^(?P<sender>[^:：]{1,40})\s*[:：]\s*(?P<body>.*)$", lines[0])
            if match:
                sender = clean_text(match.group("sender"))
                body_lines = ([clean_text(match.group("body"))] if match.group("body") else []) + lines[1:]
        body = clean_text("\n".join(body_lines))
        if len(body) >= 2:
            messages.append(Message(None, sender, body, source))
    return messages


def parse_text_messages(text: str, source: str) -> list[Message]:
    text = clean_text(text)
    if not text:
        return []

    messages: list[Message] = []
    current_time: dt.datetime | None = None
    current_sender = ""
    current_lines: list[str] = []
    has_timestamp = False

    def flush() -> None:
        nonlocal current_time, current_sender, current_lines
        body = clean_text("\n".join(current_lines))
        if body and (current_time or current_sender):
            messages.append(Message(current_time, current_sender or "未知", body, source))
        current_time = None
        current_sender = ""
        current_lines = []

    for line in text.splitlines():
        match = DATE_ANY_RE.search(line)
        if match:
            flush()
            has_timestamp = True
            current_time = parse_timestamp(match.group(0))
            current_sender, inline = _parse_inline_sender(
                line[: match.start()], line[match.end() :]
            )
            if inline:
                current_lines.append(inline)
            continue
        if current_time or current_sender:
            if line.strip():
                current_lines.append(line.rstrip())
        else:
            # Keep pre-header text so timestamp-free exports still have a fallback.
            current_lines.append(line.rstrip())

    flush()
    if messages:
        return messages
    if has_timestamp:
        return []
    return _fallback_text_messages(text, source)


def html_to_text(value: str) -> str:
    parser = HTMLTextExtractor()
    parser.feed(value)
    parser.close()
    return parser.get_text()


def parse_mhtml_messages(data: bytes, source: str) -> list[Message]:
    raw_message = email.message_from_bytes(data, policy=policy.default)
    chunks: list[str] = []
    for part in raw_message.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            decoded = payload.decode(charset, errors="replace")
        except LookupError:
            decoded = decode_bytes(payload)
        if content_type == "text/plain":
            chunks.append(decoded)
        elif content_type == "text/html":
            chunks.append(html_to_text(decoded))
    return parse_text_messages("\n".join(chunks), source)


def load_messages_from_bytes(data: bytes, suffix: str, source: str) -> list[Message]:
    suffix = suffix.lower()
    if suffix == ".json":
        try:
            payload = json.loads(decode_bytes(data))
        except json.JSONDecodeError:
            return parse_text_messages(decode_bytes(data), source)
        return walk_json(payload, find_group_name(payload) or source)
    if suffix in {".mht", ".mhtml"}:
        return parse_mhtml_messages(data, source)
    text = decode_bytes(data)
    if suffix in {".html", ".htm"}:
        text = html_to_text(text)
    return parse_text_messages(text, source)


def load_messages_from_file(path: Path) -> list[Message]:
    if path.suffix.lower() == ".zip":
        messages: list[Message] = []
        with zipfile.ZipFile(path) as archive:
            names = [
                name for name in archive.namelist()
                if Path(name).suffix.lower() in SUPPORTED_EXTS - {".zip"}
                and "resources/" not in name.replace("\\", "/").lower()
            ]
            for name in names[:50]:
                messages.extend(
                    load_messages_from_bytes(
                        archive.read(name), Path(name).suffix, f"{path.name}:{name}"
                    )
                )
        return messages
    return load_messages_from_bytes(path.read_bytes(), path.suffix, path.name)


def collect_input_files(path: Path, window_minutes: int) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(path)

    candidates = [
        item for item in path.rglob("*")
        if item.is_file()
        and item.suffix.lower() in SUPPORTED_EXTS
        and "resources" not in {part.lower() for part in item.parts}
        and "digest" not in item.name.lower()
    ]
    if not candidates:
        return []
    candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    newest_mtime = candidates[0].stat().st_mtime
    recent = [
        item for item in candidates
        if newest_mtime - item.stat().st_mtime <= window_minutes * 60
    ]
    return recent[:100] or candidates[:1]


def dedupe_messages(messages: Iterable[Message]) -> list[Message]:
    result: list[Message] = []
    seen: set[tuple[str, str, str]] = set()
    for message in messages:
        stamp = message.timestamp.strftime("%Y-%m-%d %H:%M") if message.timestamp else ""
        key = (stamp, message.sender.strip(), normalize_for_match(message.text))
        if key in seen:
            continue
        seen.add(key)
        result.append(message)
    return result


def filter_by_time(
    messages: Iterable[Message],
    since: dt.datetime | None,
    until: dt.datetime | None,
) -> list[Message]:
    result: list[Message] = []
    for message in messages:
        if message.timestamp is None:
            if since is None and until is None:
                result.append(message)
            continue
        if since and message.timestamp < since:
            continue
        if until and message.timestamp >= until:
            continue
        result.append(message)
    return result


def filter_by_groups(messages: Iterable[Message], group_spec: str) -> list[Message]:
    tokens = [
        token.strip().casefold()
        for token in re.split(r"[,，;；]", group_spec or "")
        if token.strip()
    ]
    if not tokens:
        return list(messages)
    return [
        message for message in messages
        if any(token in message.source.casefold() for token in tokens)
    ]


def _parse_clock_number(value: str) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    if text == "半":
        return 30
    if text.isdigit():
        return int(text)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if "十" not in text:
        number = 0
        for char in text:
            if char not in digits:
                return None
            number = number * 10 + digits[char]
        return number
    left, _, right = text.partition("十")
    tens = _parse_clock_number(left) if left else 1
    ones = digits.get(right, 0) if right else 0
    if tens is None:
        return None
    return tens * 10 + ones


def _parse_clock(value: str) -> tuple[int, int] | None:
    for match in CLOCK_RE.finditer(value or ""):
        period = match.group("period") or ""
        hour = _parse_clock_number(match.group("hour"))
        minute_text = match.group("minute") or ""
        minute = _parse_clock_number(minute_text) if minute_text else 0
        if hour is None or minute is None:
            continue
        if period in {"下午", "傍晚", "晚上", "夜里", "晚"} and hour < 12:
            hour += 12
        elif period == "中午" and hour < 11:
            hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    return None


def _build_datetime(date_value: dt.date, after: str) -> dt.datetime:
    clock = _parse_clock(after)
    if clock:
        return dt.datetime.combine(date_value, dt.time(clock[0], clock[1]))
    return dt.datetime.combine(date_value, dt.time(23, 59))


def is_colloquial_question(text: str) -> bool:
    """识别聊天里常见的疑问句。

    问号和“吗/呢/吧/昂”等口语收尾通常表示询问或讨论；真正的通知可
    通过 has_directive_notice() 重新保留。
    """
    value = clean_text(text)
    if not value:
        return False
    if re.search(r"[?？]", value):
        return True
    tail = re.sub(r"[\s~～!！。.，,；;…]+", "", value).rstrip()
    tail = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]+$", "", tail)
    if re.search(r"(?:吗|嘛|么|呢|吧|昂|哦|噢|呀|啊|哈)$", tail):
        return True
    return bool(re.search(r"(?:吗|嘛)", value))


def has_directive_notice(text: str) -> bool:
    """判断文本是否包含明确的行动要求，而不是单纯讨论。"""
    value = clean_text(text)
    if not value:
        return False
    if re.search(r"@全体成员|全体同学|各位同学|全员", value):
        return True
    # “请问/请教”是把“请”用作礼貌问句，不属于行动通知。
    if re.search(r"请问|请教|想问|问一下|问下", value):
        value = re.sub(r"请问|请教|想问|问一下|问下", "", value, count=1)
    return any(word in value for word in DIRECTIVE_WORDS)


def extract_deadline(message: Message) -> dt.datetime | None:
    text = message.text
    reference = message.timestamp or dt.datetime.now()
    candidates: list[dt.datetime] = []

    if is_colloquial_question(text) and not has_directive_notice(text):
        return None  # 闲聊里的“明天”不是截止时间

    range_joiners = ("—", "–", "~", "～", "至", "到")

    def is_range_start(end: int) -> bool:
        return text[end:].lstrip().startswith(range_joiners)

    for match in FULL_TS_RE.finditer(text):
        if is_range_start(match.end()):
            continue  # 日期区间只取结束日期作为截止
        try:
            value = dt.datetime(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
                int(match.group("hour") or 0),
                int(match.group("minute") or 0),
                int(match.group("second") or 0),
            )
            if not match.group("hour"):
                value = value.replace(hour=23, minute=59)
            candidates.append(value)
        except (TypeError, ValueError):
            continue

    short_pattern = re.compile(r"(?<!\d)(?P<month>\d{1,2})[月/-](?P<day>\d{1,2})日?")
    for match in short_pattern.finditer(text):
        if is_range_start(match.end()):
            continue
        try:
            month = int(match.group("month"))
            day = int(match.group("day"))
            value_date = dt.date(reference.year, month, day)
            if value_date < reference.date() - dt.timedelta(days=90):
                value_date = value_date.replace(year=reference.year + 1)
            candidates.append(_build_datetime(value_date, text[match.end(): match.end() + 80]))
        except (TypeError, ValueError):
            continue

    relative_days = {
        "今天": 0, "今日": 0, "今晚": 0,
        "明天": 1, "明日": 1, "明晚": 1,
        "后天": 2, "大后天": 3,
    }
    for word, offset in relative_days.items():
        position = text.find(word)
        if position < 0:
            continue
        tail = text[position: position + 40]
        stop = len(tail)
        for mark in ("。", "\n"):
            found = tail.find(mark)
            if 0 <= found < stop:
                stop = found
        lead = tail[:stop]
        if ("？" in lead or "?" in lead) and any(
            word in lead for word in ("几点", "什么时候", "何时", "是否", "能不能", "可不可以")
        ) and not has_directive_notice(text):
            continue  # 疑问句里的“明天/今晚”通常不是截止时间
        if is_colloquial_question(lead) and not has_directive_notice(text):
            continue
        target_date = reference.date() + dt.timedelta(days=offset)
        suffix = text[position + len(word):]
        if word in {"今晚", "明晚"}:
            suffix = "晚上" + suffix
        candidates.append(_build_datetime(target_date, suffix))

    weekday_values = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    for match in re.finditer(r"(?P<prefix>本周|这周|下周|周|星期)(?P<day>[一二三四五六日天])", text):
        target = weekday_values[match.group("day")]
        prefix = match.group("prefix")
        if prefix == "下周":
            start = reference.date() + dt.timedelta(days=7 - reference.weekday())
            value_date = start + dt.timedelta(days=target)
        elif prefix in {"本周", "这周"}:
            value_date = reference.date() - dt.timedelta(days=reference.weekday()) + dt.timedelta(days=target)
        else:
            value_date = reference.date() + dt.timedelta(days=(target - reference.weekday()) % 7)
        candidates.append(_build_datetime(value_date, text[match.end():]))

    future = [
        candidate for candidate in candidates
        if candidate.date() >= reference.date() - dt.timedelta(days=1)
    ]
    if not future:
        return None
    return min(future)


def _matches_any(text: str, words: Iterable[str]) -> list[str]:
    return [word for word in words if word in text]


def analyze_message(message: Message) -> dict[str, Any]:
    text = message.text
    sender = message.sender
    haystack = f"{sender} {text}"
    score = 0
    tags: set[str] = set()

    urgent = _matches_any(text, URGENT_WORDS)
    action = _matches_any(text, ACTION_WORDS)
    academic = _matches_any(text, ACADEMIC_WORDS)
    admin = _matches_any(text, ADMIN_WORDS)
    authority = _matches_any(sender, AUTHORITY_WORDS)

    if urgent:
        score += 3
        tags.add("urgent")
    if action:
        score += 2
        tags.add("action")
    if academic:
        score += 2
        tags.add("academic")
    if admin:
        score += 1
        tags.add("notice")
    if authority:
        score += 2
        tags.add("authority")
    if re.search(r"@全体成员|全体同学|各位同学|请各位|全员", text):
        score += 1
        tags.add("broadcast")

    deadline = extract_deadline(message)
    if deadline:
        score += 1
        tags.add("deadline")
        if deadline <= (message.timestamp or dt.datetime.now()) + dt.timedelta(days=1):
            score += 1
            tags.add("urgent")

    if URL_RE.search(text):
        score += 1
        tags.add("link")
    if len(text) >= 80:
        score += 1
        tags.add("long")

    plain = clean_text(text)
    if CHATTER_RE.fullmatch(plain):
        score -= 6
        tags.add("chatter")
    if len(plain) <= 5 and not any((urgent, action, academic, admin)):
        score -= 3
    if plain in {"收到", "好的", "谢谢"}:
        score -= 4

    colloquial_question = is_colloquial_question(text) and not has_directive_notice(text)
    if colloquial_question:
        score -= 6
        tags.add("chatter")
        tags.discard("urgent")

    if "urgent" in tags:
        category = "urgent"
    elif colloquial_question:
        category = "info"
    elif action:
        category = "action"
    elif academic:
        category = "academic"
    else:
        category = "info"

    return {
        "message": message,
        "score": score,
        "tags": tags,
        "category": category,
        "deadline": deadline,
        "urgent": urgent,
        "action_words": action,
        "academic_words": academic,
        "admin_words": admin,
        "colloquial_question": colloquial_question,
    }


def _clean_summary(text: str) -> str:
    value = clean_text(text)
    value = re.sub(r"@全体成员\s*", "", value)
    value = re.sub(r"\[(?:图片|文件)[^\]]*\]", "", value)
    value = URL_RE.sub("", value)
    return clean_text(value)


def local_summary(text: str, limit: int = 130) -> str:
    value = _clean_summary(text)
    if not value:
        return "[无文本内容]"
    sentences = [item.strip() for item in re.split(r"(?<=[。！？!?；;\n])", value) if item.strip()]
    if not sentences:
        return value[:limit]
    priority_words = URGENT_WORDS + ACTION_WORDS + ACADEMIC_WORDS
    ranked = sorted(
        enumerate(sentences),
        key=lambda pair: (
            not any(word in pair[1] for word in priority_words),
            pair[0],
        ),
    )
    return ranked[0][1][:limit].strip()


def local_action(text: str, limit: int = 110) -> str:
    value = _clean_summary(text)
    sentences = [item.strip() for item in re.split(r"(?<=[。！？!?；;\n])", value) if item.strip()]
    for sentence in sentences:
        if any(word in sentence for word in ACTION_WORDS):
            return sentence[:limit]
    return ""


def short_summary(text: str, limit: int = 60) -> str:
    """把一段文本压成适合手机推送的单行短摘要。"""
    value = _clean_summary(text)
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip("，。；;、 ") + "…"


def item_group(item: dict[str, Any]) -> str:
    """条目所属群名：优先用记录里的群名，退回消息来源。"""
    name = str(item.get("group_name") or "").strip()
    if name:
        return name
    return str(getattr(item.get("message"), "source", "") or "").strip()


def similar_text(a: str, b: str, threshold: float = 0.88) -> bool:
    """归一化后的近似判断：完全相同、互相包含或相似度达标。"""
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 20 and len(b) >= 20 and (a in b or b in a):
        return True
    return difflib.SequenceMatcher(None, a[:400], b[:400]).ratio() >= threshold


def dedupe_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda value: value["score"], reverse=True):
        text = normalize_for_match(item["message"].text)
        matched: dict[str, Any] | None = None
        for existing in result:
            other = normalize_for_match(existing["message"].text)
            if similar_text(text, other):
                matched = existing
                break
        group = item_group(item)
        if matched is None:
            item["duplicate_senders"] = [item["message"].sender]
            item["duplicate_groups"] = [group] if group else []
            result.append(item)
        else:
            matched.setdefault("duplicate_senders", []).append(item["message"].sender)
            groups = matched.setdefault("duplicate_groups", [])
            if group and group not in groups:
                groups.append(group)
    return result


def extract_links(text: str) -> list[str]:
    links: list[str] = []
    for match in URL_RE.findall(text):
        link = match.rstrip(".,;，。；、")
        if link not in links:
            links.append(link)
    return links


def format_deadline(value: dt.datetime | None) -> str:
    if not value:
        return ""
    if value.hour or value.minute:
        return value.strftime("%Y-%m-%d %H:%M")
    return value.strftime("%Y-%m-%d")


def render_markdown(
    paths: list[Path],
    all_messages: list[Message],
    filtered_messages: list[Message],
    items: list[dict[str, Any]],
    since: dt.datetime | None,
    until: dt.datetime | None,
    llm_used: bool,
) -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    if since and until:
        period = f"{since:%Y-%m-%d %H:%M} ~ {until:%Y-%m-%d %H:%M}"
    elif since:
        period = f"{since:%Y-%m-%d %H:%M} 之后"
    else:
        period = "自动识别时间范围"

    lines = [
        "# QQ 群通知摘要",
        "",
        f"- 生成时间：{now}",
        f"- 时间范围：{period}",
        f"- 输入文件：{len(paths)} 个",
        f"- 消息统计：原始 {len(all_messages)} 条，范围内 {len(filtered_messages)} 条，"
        f"重要候选 {len(items)} 条，忽略闲聊 {max(0, len(filtered_messages) - len(items))} 条",
        f"- 摘要模式：{'百炼大模型 + 本地规则' if llm_used else '纯本地规则'}",
        "",
    ]

    sections = [
        ("urgent", "今天 / 明天必须处理"),
        ("action", "需要行动"),
        ("academic", "教务与学习"),
        ("info", "仅需知悉"),
    ]
    labels = {"urgent": "紧急", "action": "行动", "academic": "学习", "info": "信息"}
    for category, title in sections:
        selected = [
            item for item in items
            if item.get("category", item["category"]) == category
        ]
        if not selected:
            continue
        lines.extend([f"## {title}", ""])
        for index, item in enumerate(selected, start=1):
            message: Message = item["message"]
            summary = item.get("summary") or local_summary(message.text)
            action = item.get("action") or local_action(message.text)
            deadline = item.get("deadline_dt")
            if not isinstance(deadline, dt.datetime):
                raw_deadline = item.get("deadline")
                deadline = raw_deadline if isinstance(raw_deadline, dt.datetime) else parse_timestamp(raw_deadline)
            source_time = message.timestamp.strftime("%m-%d %H:%M") if message.timestamp else "无时间"
            lines.append(f"### {index}. {summary}")
            meta = f"- 类别：{labels.get(item.get('category'), '信息')}；来源：{message.sender}（{source_time}）"
            if deadline:
                meta += f"；截止：{format_deadline(deadline)}"
            lines.append(meta)
            if action and normalize_for_match(action) != normalize_for_match(summary):
                lines.append(f"- 需要做：{action}")
            links = extract_links(message.text)
            if links:
                lines.append("- 链接：" + "；".join(links[:3]))
            original = clean_text(message.text).replace("\n", " ")[:260]
            if original and normalize_for_match(original) != normalize_for_match(summary):
                lines.append(f"- 原文：{original}")
            lines.append("")

    if not items:
        lines.extend(["## 结果", "", "这一时间段没有筛出需要优先阅读的通知。", ""])
    return "\n".join(lines).rstrip() + "\n"


def build_llm_prompt(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        message = item["message"]
        payload.append(
            {
                "id": index,
                "time": message.timestamp.strftime("%Y-%m-%d %H:%M:%S") if message.timestamp else None,
                "sender": message.sender,
                "text": message.text[:1200],
                "local_category": item["category"],
                "local_deadline": format_deadline(item.get("deadline")),
            }
        )
    return payload


def build_refine_messages(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """构造候选精炼的 chat messages（与具体供应商无关）。"""
    return [
        {
            "role": "system",
            "content": (
                "你是大学生QQ群通知精简助手。只根据用户提供的消息工作，不猜测、不补造。"
                "必须完整保留适用对象、条件、例外和不同人群的差异，不得把部分同学的通知概括成所有同学。"
                "把闲聊和重复内容合并，只输出严格JSON对象，不要Markdown代码块。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请把下列消息改写成适合手机推送的中文短摘要，输出 JSON："
                '{"items":[{"id":1,"category":"urgent|action|academic|info",'
                '"importance":1,"summary":"完整短摘要","audience":"适用对象",'
                '"condition":"适用条件，没有则为空字符串","action":"需要做什么，没有则为空字符串",'
                '"details":["按不同人群/条件/例外分别保留的要点"],'
                '"deadline":"YYYY-MM-DD HH:MM 或 YYYY-MM-DD，没有则为空字符串","keep":true}]}\n'
                "当前日期：" + dt.datetime.now().strftime("%Y-%m-%d %H:%M") + "。"
                "要求：summary 不超过80个汉字，必须保留适用对象、条件和关键动作；"
                "不要照抄原文，不要复述客套话，不要写“通知”“请注意”等空话。"
                "audience 写清楚是谁，例如“刚转专业到本学院的同学；其他同学”；无法判断时写“未明确”。"
                "condition 写清楚前提，例如“体测系统没有2026年成绩”；没有条件则留空。"
                "details 必须按原文顺序分条保留不同人群、条件、例外、地点、附件、链接和注意事项，不得合并掉差异。"
                "action 不超过60个汉字，没有明确行动就留空。"
                "链接、附件名、地点如果重要，必须写进 summary、details 或 action；完整链接由程序单独保留。"
                "category 规则：urgent=今天或明天必须处理；action=需要报名/提交/缴费等；"
                "academic=考试/课程/教务；info=其他值得知悉的通知。"
                "importance 为 1-5，越高越重要。keep=false 表示纯闲聊、重复或可忽略。\n"
                "严禁把只针对部分人的要求写成“所有同学”；严禁遗漏截止时间、附件和例外条件。\n\n消息："
                + json.dumps(build_llm_prompt(selected), ensure_ascii=False)
            ),
        },
    ]


def refine_items(
    items: list[dict[str, Any]],
    provider: "providers.LLMProvider",
    *,
    retries: int = 2,
    backoff: float = 1.5,
) -> list[dict[str, Any]]:
    """用 provider 精炼候选条目：只改摘要相关字段，超出 50 条的尾部原样保留。

    传输、JSON 解析与重试都在 provider 层完成，这里不碰 HTTP——
    所以换模型供应商不需要改本文件（Roadmap A4）。
    """
    if not items:
        return items
    selected = items[:50]
    data = providers.complete_json_with_retries(
        provider,
        build_refine_messages(selected),
        retries=retries,
        backoff=backoff,
        label="文本模型精炼",
        temperature=0.1,
        max_tokens=3000,
    )

    refinements = {
        int(entry["id"]): entry
        for entry in data.get("items", [])
        if isinstance(entry, dict) and str(entry.get("id", "")).isdigit()
    }
    kept: list[dict[str, Any]] = []
    for index, item in enumerate(selected, start=1):
        update = refinements.get(index)
        if not update:
            kept.append(item)
            continue
        if update.get("keep") is False:
            continue
        category = str(update.get("category") or item["category"])
        if category not in {"urgent", "action", "academic", "info"}:
            category = item["category"]
        item["category"] = category
        raw_details = update.get("details")
        if isinstance(raw_details, list):
            details = [short_summary(str(value), 300) for value in raw_details if str(value).strip()][:8]
        elif isinstance(raw_details, str) and raw_details.strip():
            details = [short_summary(raw_details, 300)]
        else:
            details = []
        item["summary"] = short_summary(str(update.get("summary") or ""), 140)
        item["audience"] = short_summary(str(update.get("audience") or ""), 100)
        item["condition"] = short_summary(str(update.get("condition") or ""), 140)
        item["details"] = details
        item["action"] = short_summary(str(update.get("action") or ""), 80)
        item["importance"] = int(update.get("importance") or 3)
        local_deadline = item.get("deadline_dt") or item.get("deadline")
        llm_deadline = parse_timestamp(update.get("deadline"))
        item["deadline_dt"] = local_deadline or llm_deadline
        kept.append(item)
    kept.extend(items[50:])
    kept.sort(
        key=lambda value: (
            {"urgent": 0, "action": 1, "academic": 2, "info": 3}.get(value.get("category"), 4),
            -int(value.get("importance") or 0),
            -value["score"],
        )
    )
    return kept


def refine_with_dashscope(
    items: list[dict[str, Any]],
    api_key: str,
    model: str,
    endpoint: str,
    timeout: int,
    *,
    retries: int = 2,
    backoff: float = 1.5,
) -> list[dict[str, Any]]:
    """兼容旧签名：按参数现场构造一个 OpenAI 兼容 Provider。

    新代码请用 `refine_items(items, providers.build_provider(settings))`，
    这样「换供应商」才只需要改配置。
    """
    provider = providers.OpenAICompatProvider(
        api_key=api_key,
        endpoint=endpoint,
        model=model,
        timeout=timeout,
    )
    return refine_items(items, provider, retries=retries, backoff=backoff)


def sort_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    category_order = {"urgent": 0, "action": 1, "academic": 2, "info": 3}
    return sorted(
        items,
        key=lambda item: (
            category_order.get(item.get("category", "info"), 4),
            -(item.get("importance") or 0),
            -item["score"],
            item.get("deadline") or dt.datetime.max,
            item["message"].timestamp or dt.datetime.max,
        ),
    )


def parse_cli_datetime(value: str) -> dt.datetime:
    parsed = parse_timestamp(value)
    if not parsed:
        raise argparse.ArgumentTypeError(f"无法解析时间: {value}")
    return parsed


def default_output_path(input_path: Path, since: dt.datetime | None) -> Path:
    label = since.strftime("%Y%m%d") if since else dt.date.today().strftime("%Y%m%d")
    if input_path.is_file():
        return input_path.with_name(f"{input_path.stem}-digest-{label}.md")
    return input_path / f"qq-digest-{label}.md"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把 QQ 群导出消息整理成通知摘要")
    parser.add_argument("--input", required=True, help="导出文件、目录或 ZIP")
    parser.add_argument("--output", help="输出 Markdown 路径；使用 - 表示打印到终端")
    parser.add_argument("--date", help="只处理指定自然日，例如 2026-09-27")
    parser.add_argument("--since", type=parse_cli_datetime, help="起始时间（含）")
    parser.add_argument("--until", type=parse_cli_datetime, help="结束时间（不含）")
    parser.add_argument("--last-hours", type=int, default=24, help="未指定日期时处理最近多少小时")
    parser.add_argument("--batch-window-minutes", type=int, default=120, help="目录模式下视为同一批的修改时间窗口")
    parser.add_argument("--min-score", type=int, default=3, help="本地规则最低分")
    parser.add_argument("--groups", help="只保留群名或文件名包含关键词的群，多个用逗号分隔")
    parser.add_argument("--max-items", type=int, default=60, help="最多输出候选数")
    parser.add_argument("--include-low", action="store_true", help="附带低分但可能相关的消息")
    parser.add_argument("--llm", action="store_true", help="调用百炼 OpenAI 兼容接口精炼摘要")
    parser.add_argument("--api-key", help="百炼 API Key；默认读取 DASHSCOPE_API_KEY")
    parser.add_argument("--model", default="qwen-plus", help="百炼模型名称")
    parser.add_argument(
        "--endpoint",
        default="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        help="OpenAI 兼容接口地址",
    )
    parser.add_argument("--timeout", type=int, default=60, help="LLM 请求超时秒数")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_path = Path(args.input).expanduser().absolute()
    try:
        paths = collect_input_files(input_path, args.batch_window_minutes)
    except (FileNotFoundError, OSError) as error:
        print(f"输入路径不可用: {error}", file=sys.stderr)
        return 2
    if not paths:
        print("没有找到可读取的 QQ 消息导出文件。", file=sys.stderr)
        return 2

    all_messages: list[Message] = []
    errors: list[str] = []
    for path in paths:
        try:
            all_messages.extend(load_messages_from_file(path))
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            errors.append(f"{path.name}: {error}")
    all_messages = dedupe_messages(all_messages)
    if args.groups:
        all_messages = filter_by_groups(all_messages, args.groups)
        if not all_messages:
            print(f"没有找到匹配群名或文件名的消息: {args.groups}", file=sys.stderr)
            return 2
    if not all_messages:
        print("文件已读取，但没有解析到消息。把一小段导出样例发给助手即可适配格式。", file=sys.stderr)
        for error in errors[:5]:
            print(f"- {error}", file=sys.stderr)
        return 2

    timestamps = [message.timestamp for message in all_messages if message.timestamp]
    latest = max(timestamps) if timestamps else dt.datetime.now()

    since = args.since
    until = args.until
    if args.date:
        try:
            day = dt.date.fromisoformat(args.date)
        except ValueError:
            print("--date 必须是 YYYY-MM-DD。", file=sys.stderr)
            return 2
        since = dt.datetime.combine(day, dt.time.min)
        until = since + dt.timedelta(days=1)
    elif since is None:
        since = latest - dt.timedelta(hours=max(1, args.last_hours))

    filtered = filter_by_time(all_messages, since, until)
    if not filtered:
        filtered = [message for message in all_messages if message.timestamp and message.timestamp.date() == latest.date()]
    if not filtered:
        filtered = all_messages

    analyses = [analyze_message(message) for message in filtered]
    candidates = [item for item in analyses if item["score"] >= args.min_score]
    if args.include_low:
        candidates.extend(
            item for item in analyses
            if item["score"] < args.min_score and not item["tags"] == {"chatter"}
        )
    candidates = dedupe_items(candidates)
    candidates.sort(key=lambda item: item["score"], reverse=True)
    candidates = candidates[: max(1, args.max_items)]

    llm_used = False
    if args.llm:
        api_key = args.api_key or ""
        if not api_key:
            import os
            api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if not api_key:
            print("已指定 --llm，但未找到 DASHSCOPE_API_KEY。", file=sys.stderr)
            return 2
        try:
            candidates = refine_with_dashscope(
                candidates, api_key, args.model, args.endpoint, args.timeout
            )
            llm_used = True
        except RuntimeError as error:
            print(f"LLM 精炼失败，已回退到本地规则：{error}", file=sys.stderr)

    candidates = sort_items(candidates)
    markdown = render_markdown(paths, all_messages, filtered, candidates, since, until, llm_used)

    if args.output == "-":
        print(markdown)
    else:
        output_path = Path(args.output).expanduser().absolute() if args.output else default_output_path(input_path, since)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
        print(f"摘要已生成: {output_path}")
        print(
            f"读取 {len(all_messages)} 条，范围内 {len(filtered)} 条，"
            f"输出候选 {len(candidates)} 条。"
        )

    if errors:
        print("部分文件读取失败：", file=sys.stderr)
        for error in errors[:5]:
            print(f"- {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
