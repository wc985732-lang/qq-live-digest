"""事件级跨群聚合（Roadmap A11）。

同一个事件常被多个群先后转发。字面相似度去重（`qq_digest.dedupe_items`）能挡住
逐字复制的，却挡不住换个说法、换个人发的同一条事。这里再用结构化事件键合一次：

    event_key = 对象 + 动作 + 时间 + 截止 + 来源

- 对象：正文里的双字词（去停用词后），换说法也能靠重叠度对上
- 动作：`category` 与行动词归一成的桶（报名 / 缴费 / 提交 …）
- 时间：正文里出现的日期（活动发生时间）
- 截止：解析出的 deadline（截止时间）
- 来源：正文抬头【学院通知】这类机构名，没有则留空

判定「同一事件」不是键的字符串相等，而是各要素的结构化相容：动作 / 截止完全一致，
时间 / 来源至少一方为空或相交，对象词重叠达标。这样「换了说法的同一条事」合得起来，
「同一天的两件不同事」不会误合。

纯函数、可复现：同一批候选 + 同一份拆分覆盖 → 结果一致。误合并时用 CLI 把该
event_key 记一条拆分覆盖（`store.event_splits`），之后这个事件不再自动合并。
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Iterable, Mapping

import qq_digest

CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
ASCII_WORD = re.compile(r"[a-z0-9]{3,}")

# 对象词里没有区分度的双字词（虚词 / 套话 / 时间词）
STOP_GRAMS = frozenset(
    """
    关于 通知 公告 各位 同学 大家 老师 我们 你们 他们 相关 有关 事项 事宜 安排 要求
    如下 内容 具体 详情 详见 注意 提醒 尽快 及时 可以 需要 必须 应当 如果 因为 所以
    以及 并且 但是 时间 地点 方式 方法 办法 进行 开展 组织 举行 举办 参加 参与 收到
    今天 明天 后天 昨天 本周 下周 本月 下月 上午 下午 晚上 中午 统一 以上 以下
    """.split()
)

# 正文里的日期 / 时刻 / 相对日
DATE_PATTERNS = (
    re.compile(r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?"),
    re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*日"),
    re.compile(r"\d{1,2}\s*[:：]\s*\d{2}"),
    re.compile(r"(?:大?后天|今天|明天|本周[一二三四五六日天]|下周[一二三四五六日天]|周[一二三四五六日天])"),
)

# 正文抬头里的机构名，如【学院通知】
HEADER_RE = re.compile(r"[【\[（(]\s*([^】\]）)]{2,20})\s*[】\]）)]")

# 抬头里要剥掉的通用后缀 / 纯修饰词，剥完才算「来源」
HEADER_SUFFIXES = ("通知", "公告", "须知", "消息", "提醒", "提示", "办公室", "委员会")
HEADER_NOISE = frozenset(
    {"重要", "紧急", "注意", "提醒", "公告", "通知", "须知", "提示", "温馨提示", "转发", "分享"}
)

# 行动词 → 动作桶（同一桶视为同类动作）
ACTION_BUCKETS = {
    "报名": "signup", "申请": "signup", "登记": "signup", "征集": "signup",
    "缴费": "pay", "交费": "pay", "收费": "pay", "付款": "pay",
    "提交": "submit", "上交": "submit", "报送": "submit", "递交": "submit",
    "领取": "collect", "发放": "collect", "回收": "collect",
    "考试": "exam", "测试": "exam", "考核": "exam", "补考": "exam",
    "会议": "meeting", "开会": "meeting", "讲座": "meeting", "报告会": "meeting", "答辩": "defence",
    "选课": "course", "退课": "course", "上课": "course", "调课": "course", "停课": "course",
    "面试": "interview", "体检": "medical", "借阅": "borrow", "归还": "borrow",
}

MIN_SHARED_GRAMS = 3
JACCARD_THRESHOLD = 0.5


def object_grams(text: str) -> frozenset[str]:
    """正文里的实义双字词（外加较长的英文 / 数字词），用作「对象」判据。"""
    value = qq_digest.normalize_for_match(text)
    grams: set[str] = set()
    for run in CJK_RUN.findall(value):
        for index in range(len(run) - 1):
            gram = run[index : index + 2]
            if gram not in STOP_GRAMS:
                grams.add(gram)
    for word in ASCII_WORD.findall(value):
        grams.add(word)
    return frozenset(grams)


def time_tokens(text: str) -> tuple[str, ...]:
    """正文里出现的日期 / 时刻 / 相对日，用作「时间」判据。"""
    found: set[str] = set()
    for pattern in DATE_PATTERNS:
        for match in pattern.finditer(str(text or "")):
            found.add(re.sub(r"\s+", "", match.group(0)).replace("：", ":"))
    return tuple(sorted(found))


def source_tokens(text: str) -> tuple[str, ...]:
    """正文抬头里的机构名（如【学院通知】），用作「来源」判据。"""
    names: set[str] = set()
    for match in HEADER_RE.finditer(str(text or "")):
        value = re.sub(r"[\s·、,，]+", "", match.group(1))
        for suffix in HEADER_SUFFIXES:
            if value.endswith(suffix) and len(value) > len(suffix):
                value = value[: -len(suffix)]
        if value and value not in HEADER_NOISE:
            names.add(value)
    return tuple(sorted(names))


def _deadline_day(item: Mapping[str, Any]) -> str:
    value = item.get("deadline_dt") or item.get("deadline")
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d")
    return ""


def action_bucket(item: Mapping[str, Any]) -> str:
    """把「动作」归一到一个桶：先看行动词，再退回 category。"""
    buckets = {
        ACTION_BUCKETS[str(word).strip()]
        for word in (item.get("action_words") or ())
        if str(word).strip() in ACTION_BUCKETS
    }
    if buckets:
        return sorted(buckets)[0]
    return str(item.get("category") or "")


def event_parts(item: Mapping[str, Any]) -> dict[str, Any]:
    """拆出构成事件键的五要素，供判定 / 展示 / 落库复用。"""
    text = str(item["message"].text or "")
    return {
        "action": action_bucket(item),
        "time": time_tokens(text),
        "deadline": _deadline_day(item),
        "source": source_tokens(text),
        "objects": object_grams(text),
    }


def event_key(item: Mapping[str, Any]) -> str:
    """事件键：人可读、可复制，用于 CLI 的拆分覆盖；对象词取前 12 个保证键不过长。"""
    parts = event_parts(item)
    head = "|".join(
        (
            str(parts["action"]),
            ",".join(parts["time"]),
            str(parts["deadline"]),
            ",".join(parts["source"]),
        )
    )
    return f"{head}|{','.join(sorted(parts['objects'])[:12])}"


def signature_from_key(key: str) -> dict[str, Any] | None:
    """把 event_key 还原成五要素，用于比对拆分覆盖（还原不出返回 None）。"""
    fields = str(key or "").split("|")
    if len(fields) != 5:
        return None
    action, times, deadline, sources, objects = fields
    return {
        "action": action,
        "time": tuple(part for part in times.split(",") if part),
        "deadline": deadline,
        "source": tuple(part for part in sources.split(",") if part),
        "objects": frozenset(part for part in objects.split(",") if part),
    }


def _overlap(left: Any, right: Any) -> bool:
    shared = len(set(left or ()) & set(right or ()))
    if shared < 2:
        return False
    if shared >= MIN_SHARED_GRAMS:
        return True
    union = len(set(left or ()) | set(right or ()))
    return bool(union) and shared / union >= JACCARD_THRESHOLD


def _has_structured_anchor(parts: Mapping[str, Any]) -> bool:
    """是否有日期 / 来源 / 截止这类硬锚点——只有它们才足以支撑「同一件事」的判断。"""
    return bool(parts.get("deadline") or parts.get("time") or parts.get("source"))


def compatible(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """两个五要素是否指向同一个事件（判据可解释、无副作用）。"""
    for field in ("action", "deadline"):
        a, b = str(left.get(field) or ""), str(right.get(field) or "")
        if a and b and a != b:
            return False
    for field in ("time", "source"):
        a, b = set(left.get(field) or ()), set(right.get(field) or ())
        if a and b and not (a & b):
            # 来源允许「学院」与「学院教务处」这类包含关系
            if field != "source" or not any(x in y or y in x for x in a for y in b):
                return False
    if not _overlap(left.get("objects"), right.get("objects")):
        return False
    if _has_structured_anchor(left) or _has_structured_anchor(right):
        return True
    # 双方都没有日期/来源/截止锚点：只靠双字词重叠太容易误合，要求更强的对象重合
    shared = set(left.get("objects") or ()) & set(right.get("objects") or ())
    return len(shared) >= MIN_SHARED_GRAMS + 1


def describe(item: Mapping[str, Any]) -> str:
    """把一条候选的事件键渲染成给人看的一行，供 CLI 核对。"""
    parts = event_parts(item)
    bits = [f"动作={parts['action'] or '—'}"]
    if parts["time"]:
        bits.append("时间=" + "/".join(parts["time"]))
    if parts["deadline"]:
        bits.append("截止=" + str(parts["deadline"]))
    if parts["source"]:
        bits.append("来源=" + "/".join(parts["source"]))
    bits.append("对象=" + "/".join(sorted(parts["objects"])[:6]))
    return " · ".join(bits)


def _seed_source(item: dict[str, Any]) -> None:
    """给保留项补齐来源字段，沿用既有的 duplicate_groups / senders 口径。"""
    group = qq_digest.item_group(item)
    item.setdefault("duplicate_senders", [str(item["message"].sender or "")])
    item.setdefault("duplicate_groups", [group] if group else [])
    names = item.setdefault("event_sources", [])
    for name in source_tokens(str(item["message"].text or "")):
        if name not in names:
            names.append(name)


def _absorb(primary: dict[str, Any], item: dict[str, Any]) -> None:
    """把被合并的一条挂到保留项上（群 / 发送者 / 来源去重后累加）。"""
    group = qq_digest.item_group(item)
    if group:
        groups = primary.setdefault("duplicate_groups", [])
        if group not in groups:
            groups.append(group)
    sender = str(item["message"].sender or "")
    senders = primary.setdefault("duplicate_senders", [])
    if sender and sender not in senders:
        senders.append(sender)
    names = primary.setdefault("event_sources", [])
    for name in source_tokens(str(item["message"].text or "")):
        if name not in names:
            names.append(name)
    primary["event_merged"] = True


def merge_items(
    items: Iterable[Mapping[str, Any]],
    *,
    split_keys: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把同一事件的候选合并成一条，返回（保留, 被合并）。

    保留项上挂 `duplicate_groups` / `duplicate_senders` / `event_sources` / `event_merged`；
    被合并项在列表里带 `into`（并入哪条）与 `key`（事件键），供决策日志记录。
    分值高的先占位，保证合并结果与输入顺序无关。
    """
    split_parts = [
        parts
        for parts in (signature_from_key(key) for key in (split_keys or ()))
        if parts is not None
    ]
    kept: list[dict[str, Any]] = []
    primaries: list[tuple[dict[str, Any], dict[str, Any]]] = []
    merged: list[dict[str, Any]] = []
    ordered = sorted(
        items,
        key=lambda value: (-int(value.get("score") or 0), str(value.get("msg_id") or "")),
    )
    for item in ordered:
        parts = event_parts(item)
        if any(compatible(parts, known) for known in split_parts):
            _seed_source(item)  # 用户标记过「别自动合并」，当独立一条
            kept.append(item)
            primaries.append((item, parts))
            continue
        target = next(
            (value for value, other in primaries if compatible(parts, other)), None
        )
        if target is None:
            _seed_source(item)
            kept.append(item)
            primaries.append((item, parts))
        else:
            _absorb(target, item)
            merged.append({"item": item, "into": target, "key": event_key(item)})
    return kept, merged
