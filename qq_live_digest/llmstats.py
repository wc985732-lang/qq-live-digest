"""模型用量统计词表与聚合（Roadmap A5）。

本模块只做三件**纯函数**的事，不碰数据库、不碰网络：

1. 统一「用途 / 状态 / 周期」词表（`purpose`、`status`、`period`）；
2. 把 `llm_calls` 行按日 / 周 / 月分桶并汇总（token、耗时、失败、重试、降级）；
3. 按当前配置单价把 token 折算成费用，并渲染成 CLI 表格。

费用**不入库**：`llm_calls` 只存 token 与耗时，单价在展示时套用。这样换价目表、
回溯历史成本都只需要改配置，不用迁移数据。
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterable, Mapping, Sequence

# ------------------------------------------------------------------ 用途词表
PURPOSE_REFINE = "refine"      # 群消息候选精炼（文本模型）
PURPOSE_VISION = "vision"      # 图片 / 扫描件文字识别（视觉模型）
PURPOSE_DOCUMENT = "document"  # 群文件结构化提取
PURPOSE_CHAT = "chat"          # 其余文本调用（未标注用途时的兜底）

PURPOSES = (PURPOSE_REFINE, PURPOSE_VISION, PURPOSE_DOCUMENT, PURPOSE_CHAT)

PURPOSE_LABELS = {
    PURPOSE_REFINE: "候选精炼",
    PURPOSE_VISION: "图片识别",
    PURPOSE_DOCUMENT: "文档理解",
    PURPOSE_CHAT: "其他调用",
}

# ------------------------------------------------------------------ 状态词表
STATUS_OK = "ok"            # 成功（重试后成功也算成功，attempts 会 > 1）
STATUS_ERROR = "error"      # 重试耗尽或不可重试，最终失败
STATUS_SKIPPED = "skipped"  # 本该调用但跳过了（例如没配密钥），未产生用量

STATUSES = (STATUS_OK, STATUS_ERROR, STATUS_SKIPPED)

STATUS_LABELS = {
    STATUS_OK: "成功",
    STATUS_ERROR: "失败",
    STATUS_SKIPPED: "跳过",
}

# ------------------------------------------------------------------ 路由词表（A6）
ROUTE_RULE = "rule"        # 本地规则直接交付，没调模型（分级路由的第三档）
ROUTE_LIGHT = "light"      # 轻量 / 便宜模型：清晰、量小的批次
ROUTE_STRONG = "strong"    # 高能力模型：难例才动用
ROUTE_DEFAULT = "default"  # 没启用分级路由（或早于 A6 的历史记录）

ROUTES = (ROUTE_RULE, ROUTE_LIGHT, ROUTE_STRONG, ROUTE_DEFAULT)

ROUTE_LABELS = {
    ROUTE_RULE: "本地规则",
    ROUTE_LIGHT: "轻量模型",
    ROUTE_STRONG: "高能力模型",
    ROUTE_DEFAULT: "未分级",
}

# ------------------------------------------------------------------ 周期词表
PERIOD_DAY = "day"
PERIOD_WEEK = "week"
PERIOD_MONTH = "month"

PERIODS = (PERIOD_DAY, PERIOD_WEEK, PERIOD_MONTH)

PERIOD_LABELS = {
    PERIOD_DAY: "日",
    PERIOD_WEEK: "周",
    PERIOD_MONTH: "月",
}

#: 每种周期默认展示多少个桶（旧的在前）。
DEFAULT_BUCKETS = {PERIOD_DAY: 14, PERIOD_WEEK: 8, PERIOD_MONTH: 6}

# ------------------------------------------------------------------ 小工具
def purpose_label(purpose: str) -> str:
    value = str(purpose or "")
    return PURPOSE_LABELS.get(value, value or "未标注")


def status_label(status: str) -> str:
    value = str(status or "")
    return STATUS_LABELS.get(value, value or "未知")


def route_label(route: str) -> str:
    value = str(route or "")
    return ROUTE_LABELS.get(value, value or "未分级")


def route_counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """按路由档位统计调用次数；没写 route 的记录（历史数据）算「未分级」。"""
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("route") or "") or ROUTE_DEFAULT
        counts[key] = counts.get(key, 0) + 1
    return counts


def parse_period(value: str) -> str:
    text = str(value or "").strip().lower()
    aliases = {"d": PERIOD_DAY, "日": PERIOD_DAY, "w": PERIOD_WEEK, "周": PERIOD_WEEK,
               "m": PERIOD_MONTH, "月": PERIOD_MONTH}
    text = aliases.get(text, text)
    if text not in PERIODS:
        raise ValueError(f"未知的周期：{value}（可选 day / week / month）")
    return text


def format_tokens(count: Any) -> str:
    try:
        value = int(count or 0)
    except (TypeError, ValueError):
        value = 0
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def format_cost(yuan: float, *, symbol: str = "¥") -> str:
    value = float(yuan or 0.0)
    if value == 0:
        return f"{symbol}0"
    if abs(value) < 0.01:
        return f"{symbol}{value:.4f}"
    if abs(value) < 1:
        return f"{symbol}{value:.3f}"
    return f"{symbol}{value:.2f}"


def call_cost(
    prompt_tokens: Any,
    completion_tokens: Any,
    *,
    price_in: float = 0.0,
    price_out: float = 0.0,
) -> float:
    """按「元 / 百万 token」折算一次调用（或一组汇总）的费用。"""
    return (
        float(prompt_tokens or 0) / 1_000_000 * float(price_in or 0.0)
        + float(completion_tokens or 0) / 1_000_000 * float(price_out or 0.0)
    )


def _moment(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


# ------------------------------------------------------------------ 分桶
def bucket_key(moment: dt.datetime, period: str) -> str:
    value = period if period in PERIODS else PERIOD_DAY
    if value == PERIOD_WEEK:
        year, week, _ = moment.isocalendar()
        return f"{year}-W{week:02d}"
    if value == PERIOD_MONTH:
        return f"{moment.year}-{moment.month:02d}"
    return moment.strftime("%Y-%m-%d")


def bucket_label(key: str, period: str) -> str:
    if period == PERIOD_WEEK:
        try:
            year_text, week_text = str(key).split("-W")
            start = dt.date.fromisocalendar(int(year_text), int(week_text), 1)
        except (ValueError, AttributeError):
            return str(key)
        end = start + dt.timedelta(days=6)
        return f"{key}（{start.strftime('%m-%d')}~{end.strftime('%m-%d')}）"
    return str(key)


def _shift(moment: dt.datetime, period: str, steps: int) -> dt.datetime:
    if period == PERIOD_WEEK:
        return moment - dt.timedelta(days=7 * steps)
    if period == PERIOD_MONTH:
        month_index = moment.year * 12 + (moment.month - 1) - steps
        year, month = divmod(month_index, 12)
        day = min(moment.day, 28)
        return moment.replace(year=year, month=month + 1, day=day)
    return moment - dt.timedelta(days=steps)


def recent_keys(period: str, count: int, *, now: dt.datetime | None = None) -> list[str]:
    """返回最近 `count` 个周期键，旧的在前（含当前这个未结束的）。"""
    current = now or dt.datetime.now()
    keys: list[str] = []
    for step in range(max(1, int(count)) - 1, -1, -1):
        keys.append(bucket_key(_shift(current, period, step), period))
    # 跨月/跨年时理论上可能重复，去重同时保持顺序
    seen: set[str] = set()
    unique: list[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def period_start(moment: dt.datetime, period: str) -> dt.datetime:
    """把一个时刻归到它所在周期的起点（日 → 当天 0 点，周 → 周一，月 → 1 号）。"""
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == PERIOD_WEEK:
        return midnight - dt.timedelta(days=midnight.weekday())
    if period == PERIOD_MONTH:
        return midnight.replace(day=1)
    return midnight


def window_start(period: str, count: int, *, now: dt.datetime | None = None) -> dt.datetime:
    """`recent_keys(period, count)` 覆盖区间的起点，用来限定查询范围。"""
    current = now or dt.datetime.now()
    return period_start(_shift(current, period, max(1, int(count)) - 1), period)


# ------------------------------------------------------------------ 汇总
def _empty_bucket(key: str, period: str) -> dict[str, Any]:
    return {
        "key": key,
        "label": bucket_label(key, period),
        "calls": 0,
        "ok": 0,
        "failed": 0,
        "skipped": 0,
        "retried": 0,
        "fallback": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "latency_ms": 0,
        "purposes": {},
        "models": {},
    }


def bucket_from_rows(rows: Iterable[Mapping[str, Any]], key: str, period: str) -> dict[str, Any]:
    bucket = _empty_bucket(key, period)
    for row in rows:
        _add_row(bucket, row)
    return bucket


def _add_row(bucket: dict[str, Any], row: Mapping[str, Any]) -> None:
    status = str(row.get("status") or STATUS_OK)
    bucket["calls"] += 1
    if status == STATUS_SKIPPED:
        bucket["skipped"] += 1
    elif status == STATUS_ERROR:
        bucket["failed"] += 1
    else:
        bucket["ok"] += 1
    if int(row.get("retried") or 0):
        bucket["retried"] += 1
    if int(row.get("fallback") or 0):
        bucket["fallback"] += 1
    bucket["prompt_tokens"] += int(row.get("prompt_tokens") or 0)
    bucket["completion_tokens"] += int(row.get("completion_tokens") or 0)
    bucket["latency_ms"] += int(row.get("latency_ms") or 0)
    purpose = str(row.get("purpose") or "")
    bucket["purposes"][purpose] = bucket["purposes"].get(purpose, 0) + 1
    model = str(row.get("model") or "")
    bucket["models"][model] = bucket["models"].get(model, 0) + 1


def totals_from_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    bucket = _empty_bucket("总计", PERIOD_DAY)
    for row in rows:
        _add_row(bucket, row)
    return bucket


def group_rows(
    rows: Iterable[Mapping[str, Any]],
    period: str,
    *,
    now: dt.datetime | None = None,
    count: int | None = None,
) -> list[dict[str, Any]]:
    """把明细行按周期分桶，返回旧的在前、且补齐空桶的连续序列。"""
    normalized = period if period in PERIODS else PERIOD_DAY
    limit = int(count if count is not None else DEFAULT_BUCKETS.get(normalized, 14))
    keys = recent_keys(normalized, limit, now=now)
    index = {key: _empty_bucket(key, normalized) for key in keys}
    for row in rows:
        moment = _moment(row.get("created_at"))
        if moment is None:
            continue
        key = bucket_key(moment, normalized)
        bucket = index.get(key)
        if bucket is None:
            continue  # 超出显示窗口的历史行不掺进本期视图
        _add_row(bucket, row)
    return [index[key] for key in keys]


def bucket_tokens(bucket: Mapping[str, Any]) -> int:
    return int(bucket.get("prompt_tokens") or 0) + int(bucket.get("completion_tokens") or 0)


def bucket_cost(bucket: Mapping[str, Any], *, price_in: float = 0.0, price_out: float = 0.0) -> float:
    return call_cost(
        bucket.get("prompt_tokens"),
        bucket.get("completion_tokens"),
        price_in=price_in,
        price_out=price_out,
    )


def top_errors(rows: Iterable[Mapping[str, Any]], *, limit: int = 3) -> list[tuple[str, int]]:
    """失败原因 TOP N（按出现次数），用于解释「为什么这批没走大模型」。"""
    counts: dict[str, int] = {}
    for row in rows:
        if str(row.get("status") or "") != STATUS_ERROR:
            continue
        reason = str(row.get("error") or "").strip() or "（未记录原因）"
        counts[reason] = counts.get(reason, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[: max(1, int(limit))]


def summarise_bucket(bucket: Mapping[str, Any], *, price_in: float = 0.0, price_out: float = 0.0) -> str:
    parts = [
        f"{int(bucket.get('calls') or 0)} 次调用",
        f"输入 {format_tokens(bucket.get('prompt_tokens'))} / 输出 {format_tokens(bucket.get('completion_tokens'))} token",
        f"费用 {format_cost(bucket_cost(bucket, price_in=price_in, price_out=price_out))}",
    ]
    extras = []
    if int(bucket.get("failed") or 0):
        extras.append(f"失败 {int(bucket['failed'])}")
    if int(bucket.get("retried") or 0):
        extras.append(f"重试 {int(bucket['retried'])}")
    if int(bucket.get("fallback") or 0):
        extras.append(f"降级 {int(bucket['fallback'])}")
    if int(bucket.get("skipped") or 0):
        extras.append(f"跳过 {int(bucket['skipped'])}")
    if extras:
        parts.append("、".join(extras))
    return " · ".join(parts)


def render_table(
    buckets: Sequence[Mapping[str, Any]],
    *,
    price_in: float = 0.0,
    price_out: float = 0.0,
) -> list[str]:
    """把分桶结果渲染成对齐的表格（列宽自适应，便于直接贴到 Issue / 日志）。"""
    header = ["期间", "调用", "成功", "失败", "跳过", "重试", "降级", "输入", "输出", "费用"]
    rows: list[list[str]] = []
    for bucket in buckets:
        rows.append(
            [
                str(bucket.get("label") or bucket.get("key") or ""),
                str(int(bucket.get("calls") or 0)),
                str(int(bucket.get("ok") or 0)),
                str(int(bucket.get("failed") or 0)),
                str(int(bucket.get("skipped") or 0)),
                str(int(bucket.get("retried") or 0)),
                str(int(bucket.get("fallback") or 0)),
                format_tokens(bucket.get("prompt_tokens")),
                format_tokens(bucket.get("completion_tokens")),
                format_cost(bucket_cost(bucket, price_in=price_in, price_out=price_out)),
            ]
        )
    widths = [max([len(header[index])] + [len(row[index]) for row in rows]) for index in range(len(header))]
    lines = ["  ".join(header[index].ljust(widths[index]) for index in range(len(header))).rstrip()]
    for row in rows:
        lines.append("  ".join(row[index].ljust(widths[index]) for index in range(len(header))).rstrip())
    return lines


def breakdown(entries: Mapping[str, int], *, labels: Mapping[str, str] | None = None, limit: int = 5) -> str:
    """把 {键: 次数} 渲染成一行「标签 次数」摘要（多的在前）。"""
    if not entries:
        return "（无）"
    mapping = labels or {}
    ordered = sorted(entries.items(), key=lambda item: (-int(item[1]), str(item[0])))
    parts = []
    for key, value in ordered[: max(1, int(limit))]:
        name = mapping.get(str(key)) or (str(key) or "未标注")
        parts.append(f"{name} {int(value)}")
    rest = len(ordered) - len(parts)
    if rest > 0:
        parts.append(f"其他 {rest} 类")
    return "、".join(parts)


def describe_calls(
    rows: Iterable[Mapping[str, Any]],
    *,
    price_in: float = 0.0,
    price_out: float = 0.0,
) -> list[str]:
    """把用量明细渲染成给人看的短句（供 `main.py llm-stats --recent` 复用）。"""
    lines: list[str] = []
    for row in rows:
        stamp = str(row.get("created_at") or "").replace("T", " ")[:19]
        status = status_label(str(row.get("status") or ""))
        model = str(row.get("model") or row.get("provider") or "未知模型")
        route = str(row.get("route") or "")
        if route:
            model = f"{model} · {route_label(route)}"
        tokens = int(row.get("prompt_tokens") or 0) + int(row.get("completion_tokens") or 0)
        head = (
            f"{stamp}  [{status}] {purpose_label(str(row.get('purpose') or ''))} · {model}"
            f" · {format_tokens(tokens)} token / {int(row.get('latency_ms') or 0)}ms"
        )
        cost = call_cost(
            row.get("prompt_tokens"), row.get("completion_tokens"), price_in=price_in, price_out=price_out
        )
        if cost:
            head += f" / {format_cost(cost)}"
        lines.append(head)
        bits: list[str] = []
        if int(row.get("attempts") or 0) > 1:
            bits.append(f"尝试 {int(row['attempts'])} 次")
        if int(row.get("retried") or 0):
            bits.append("重试过")
        if int(row.get("fallback") or 0):
            bits.append("已降级")
        if bits:
            lines.append("    " + " · ".join(bits))
        reason = str(row.get("error") or "").strip()
        if reason:
            lines.append(f"    原因：{reason}")
    return lines
