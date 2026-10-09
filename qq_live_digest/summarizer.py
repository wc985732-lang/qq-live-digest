"""把消息记录转成摘要条目，并生成适合推送的纯文本。"""

from __future__ import annotations

import datetime as dt
import logging
import re
import sys
from html import escape as html_escape
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:  # 允许从任意工作目录导入 qq_digest
    sys.path.insert(0, str(PROJECT_ROOT))

import qq_digest  # noqa: E402  (需要先修好 sys.path)
from qq_digest import Message  # noqa: E402

from . import decisions  # noqa: E402
from . import confidence  # noqa: E402
from . import llmstats  # noqa: E402
from . import providers  # noqa: E402
from . import routing  # noqa: E402
from .config import Settings  # noqa: E402
from .retry import llm_should_retry  # noqa: E402
from .timeutil import now_local, parse_iso  # noqa: E402

LOGGER = logging.getLogger(__name__)

CATEGORY_ORDER = {"urgent": 0, "action": 1, "academic": 2, "info": 3}
SUBSTANTIVE_TAGS = {"action", "notice", "academic", "broadcast", "authority"}
# 只有这些词才值得「立刻响一声」，避免「今天天气」这类误报
STRONG_URGENT_WORDS = ("紧急", "务必", "尽快", "马上", "立即", "最后通知", "逾期", "过期", "截止")
CATEGORY_LABEL = {"urgent": "紧急", "action": "待办", "academic": "学业", "info": "通知"}
IMPORTANCE_BY_CATEGORY = {"urgent": 5, "action": 4, "academic": 3, "info": 3}
KIND_TITLE = {
    "urgent": "紧急通知",
    "window": "群通知汇总",
    "silent": "静默窗口",
    "morning": "今日截止",
    "evening": "截止提醒",
}


@dataclass
class Digest:
    kind: str
    window_start: dt.datetime | None
    window_end: dt.datetime | None
    items: list[dict[str, Any]] = field(default_factory=list)
    message_ids: list[str] = field(default_factory=list)
    body: str = ""
    summary: str = ""
    html: str = ""
    llm_used: bool = False
    llm_error: str = ""
    llm_retryable: bool = False
    message_count: int = 0
    groups: list[str] = field(default_factory=list)
    id: int = 0
    decisions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_focus(self) -> bool:
        return bool(self.items)

    @property
    def title(self) -> str:
        return KIND_TITLE.get(self.kind, "群通知")

    @property
    def window_start_text(self) -> str:
        return self.window_start.strftime("%Y-%m-%d %H:%M") if self.window_start else ""

    @property
    def window_end_text(self) -> str:
        return self.window_end.strftime("%Y-%m-%d %H:%M") if self.window_end else ""

    def payload(self) -> list[dict[str, Any]]:
        """可 JSON 序列化的条目摘要。"""
        items: list[dict[str, Any]] = []
        for item in self.items:
            message: Message = item["message"]
            items.append(
                {
                    "category": item.get("category", "info"),
                    "importance": int(item.get("importance") or 0),
                    "score": int(item.get("score") or 0),
                    "summary": item.get("summary") or qq_digest.local_summary(message.text),
                    "audience": str(item.get("audience") or ""),
                    "condition": str(item.get("condition") or ""),
                    "details": [str(value) for value in (item.get("details") or []) if str(value).strip()],
                    "action": item.get("action") or qq_digest.local_action(message.text),
                    "evidence": item.get("evidence", ""),
                    "deadline": qq_digest.format_deadline(item.get("deadline_dt") or item.get("deadline")),
                    "group": item.get("group_name", ""),
                    "group_id": item.get("group_id", ""),
                    "sender": message.sender,
                    "text": message.text,
                    "msg_id": item.get("msg_id", ""),
                    "confidence": dict(item.get("confidence") or {}),
                    "why": str(item.get("why") or ""),
                }
            )
        return items


def record_to_message(record: dict[str, Any]) -> Message:
    stamp = parse_iso(record.get("ts")) or parse_iso(record.get("received_at")) or now_local()
    sender = str(record.get("sender_name") or record.get("sender_id") or "群成员")
    return Message(
        timestamp=stamp,
        sender=sender,
        text=str(record.get("content") or ""),
        source=str(record.get("group_name") or record.get("group_id") or ""),
    )


def payload_to_item(entry: dict[str, Any]) -> dict[str, Any]:
    """把归档 payload 还原成可再次渲染的 digest item。"""
    text = str(entry.get("text") or "")
    group = str(entry.get("group") or "")
    message = Message(
        timestamp=parse_iso(entry.get("created_at")) or now_local(),
        sender=str(entry.get("sender") or "群成员"),
        text=text,
        source=group,
    )
    category = str(entry.get("category") or "info")
    item: dict[str, Any] = {
        "message": message,
        "category": category,
        "importance": int(entry.get("importance") or IMPORTANCE_BY_CATEGORY.get(category, 3)),
        "score": int(entry.get("score") or 0),
        "summary": str(entry.get("summary") or qq_digest.local_summary(text)),
        "audience": str(entry.get("audience") or ""),
        "condition": str(entry.get("condition") or ""),
        "details": [str(value) for value in (entry.get("details") or []) if str(value).strip()],
        "action": str(entry.get("action") or qq_digest.local_action(text)),
        "evidence": str(entry.get("evidence") or ""),
        "deadline_dt": qq_digest.parse_timestamp(entry.get("deadline")),
        "group_name": group,
        "group_id": str(entry.get("group_id") or ""),
        "msg_id": str(entry.get("msg_id") or ""),
    }
    item["confidence"] = dict(entry.get("confidence") or {})
    item["why"] = str(entry.get("why") or "")
    if not item["evidence"]:
        item["evidence"] = evidence_sentence(text, item)
    return item


def analyse_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    analyses: list[dict[str, Any]] = []
    for record in records:
        analysis = qq_digest.analyze_message(record_to_message(record))
        analysis["group_name"] = str(record.get("group_name") or record.get("group_id") or "")
        analysis["group_id"] = str(record.get("group_id") or "")
        analysis["msg_id"] = str(record.get("msg_id") or "")
        analysis["record"] = record
        analyses.append(analysis)
    return analyses


def _item_group_id(item: dict[str, Any]) -> str:
    record = item.get("record")
    if not isinstance(record, dict):
        record = {}
    return str(item.get("group_id") or record.get("group_id") or "")


def _item_group_name(item: dict[str, Any]) -> str:
    message: Message | None = item.get("message")
    return str(item.get("group_name") or getattr(message, "source", "") or "")


def is_quiet_group_item(item: dict[str, Any], settings: Settings) -> bool:
    group_id = _item_group_id(item)
    if group_id and settings.is_quiet_group(group_id):
        return True
    return settings.is_quiet_group_name(_item_group_name(item))


def is_notice_item(item: dict[str, Any], settings: Settings) -> bool:
    """低优先级群里只保留像通知的内容，普通讨论不进入摘要。"""
    message: Message | None = item.get("message")
    text = str(getattr(message, "text", "") or "")
    if bool(item.get("colloquial_question")):
        return False
    if qq_digest.is_colloquial_question(text) and not qq_digest.has_directive_notice(text):
        return False
    tags = set(item.get("tags") or ())
    if tags & {"authority", "broadcast"}:
        return True
    if qq_digest.has_directive_notice(text):
        return True
    return bool(tags & {"action", "academic", "notice"})


def is_task_eligible(item: dict[str, Any], settings: Settings) -> bool:
    """历史回填、待办同步和截止提醒共用的防误报门槛。"""
    quiet = is_quiet_group_item(item, settings)
    if quiet and not is_notice_item(item, settings):
        return False
    message: Message | None = item.get("message")
    text = str(getattr(message, "text", "") or "")
    if qq_digest.is_colloquial_question(text) and not qq_digest.has_directive_notice(text):
        return False
    category = str(item.get("category") or "info")
    action = str(item.get("action") or "")
    deadline = item.get("deadline_dt") or item.get("deadline")
    if is_quiet_group_item(item, settings):
        return category in {"urgent", "action"} or bool(
            action and qq_digest.has_directive_notice(text)
        )
    return category in {"urgent", "action"} or bool(action) or bool(deadline)


WEAK_ACTION_WORDS = ("记得", "别忘了", "不要忘记", "可能要", "可能", "也许", "大概", "尽量", "有空", "考虑", "可以在")
STRONG_DIRECTIVE_WORDS = (
    "请", "务必", "必须", "要求", "按时", "尽早", "截止", "前完成", "前提交",
)
AMBIGUOUS_TIME_WORDS = ("近期", "近日", "稍后", "左右", "前后", "之前要", "尽快")


def classify_task_item(item: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """把行动项分成可直接执行的 open 和需要用户确认的 candidate。"""
    message: Message | None = item.get("message")
    text = str(getattr(message, "text", "") or "")
    category = str(item.get("category") or "info")
    action_words = [str(word) for word in (item.get("action_words") or []) if str(word)]
    deadline = item.get("deadline_dt") or item.get("deadline")
    has_deadline = isinstance(deadline, dt.datetime)
    evidence = str(item.get("evidence") or evidence_sentence(text, item))
    weak = next((word for word in WEAK_ACTION_WORDS if word in text), "")
    ambiguous = next((word for word in AMBIGUOUS_TIME_WORDS if word in text), "")
    strong_directive = any(word in text for word in STRONG_DIRECTIVE_WORDS)
    direct_action = bool(action_words) or any(
        word in text
        for word in (
            "提交", "填写", "报名", "缴费", "领取", "参加", "完成", "上传", "确认",
            "回复", "核对", "下载", "安装", "办理", "注册", "认证", "上交",
        )
    )
    urgent = category == "urgent"
    tags = set(item.get("tags") or ())
    quiet = is_quiet_group_item(item, settings)

    # 分数与「为什么」同源：权重写在触发规则里，改权重就改解释，不会各说各话。
    # 等级阈值取配置里的确认阈值，这样「低置信度进待确认」是配置驱动的（A8）。
    threshold = float(
        getattr(settings, "candidate_min_confidence", confidence.DEFAULT_LOW)
        or confidence.DEFAULT_LOW
    )
    assessment = confidence.assess_task(
        item,
        quiet=quiet,
        evidence=evidence,
        high=max(confidence.DEFAULT_HIGH, threshold),
        low=threshold,
    )

    status = "candidate"
    reason = "只有行动线索，证据还不够直接，先放到待确认。"
    if weak:
        reason = f"原话有“{weak}”等不确定措辞，先确认是否真的要办。"
    elif has_deadline and (direct_action or strong_directive):
        status = "open"
        reason = "有明确行动要求和截止时间，直接进入待办。"
    elif urgent and (direct_action or strong_directive or "urgent" in tags):
        status = "open"
        reason = "规则判断为紧急事项，直接进入待办。"
    elif strong_directive and direct_action:
        status = "open"
        reason = "有明确的行动要求，没有写截止时间。"
    elif has_deadline and not direct_action and not strong_directive:
        reason = "提到了日期，但更像活动日期而不是截止时间。"
    elif ambiguous:
        reason = f"“{ambiguous}”的时间语义不明确，先确认。"
    elif not has_deadline and direct_action:
        reason = "识别到行动，但没有明确截止时间，先确认是否真的要办。"

    # A8 硬门槛：低于确认阈值的候选绝不直接进正式待办，必须人工点头。
    if status == "open" and assessment.confidence < threshold:
        status = "candidate"
        reason = (
            f"置信度 {confidence.percent(assessment.confidence)}% 低于确认阈值 "
            f"{confidence.percent(threshold)}%，先交人工确认。"
        )

    return {
        "status": status,
        "confidence": assessment.confidence,
        "level": assessment.level,
        "triggers": list(assessment.triggers),
        "reason": reason,
        "threshold": threshold,
        "source": str(item.get("source") or "qq_message"),
    }


def focus_reason(analysis: dict[str, Any], settings: Settings) -> str:
    """没进候选时给出人话原因；命中候选返回空串。

    这里是「算不算重点」的判定本体——`is_focus` 只是它的一层薄封装，
    这样决策日志里写的原因和真实判定永远一致，不会各改各的。
    """
    if bool(analysis.get("colloquial_question")):
        return "像闲聊提问，未命中指令式通知"
    if is_quiet_group_item(analysis, settings) and not is_notice_item(analysis, settings):
        return "安静群且未命中通知关键词"
    score = int(analysis.get("score") or 0)
    if analysis.get("category") == "urgent":
        if score >= 1:
            return ""
        return f"紧急类但分值 {score} < 1"
    if score >= settings.min_score:
        return ""
    return f"分值 {score} < 阈值 {settings.min_score}"


def is_focus(analysis: dict[str, Any], settings: Settings) -> bool:
    return not focus_reason(analysis, settings)


def filter_recent_duplicates(
    analyses: list[dict[str, Any]],
    history: Iterable[dict[str, Any]] | None,
    settings: Settings,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """挑出最近几小时已推过、内容几乎一样的通知。

    班级群和闲聊群常常把同一条通知转发两遍，靠这里跨群、跨窗口去重，避免重复推送。
    """
    hours = int(settings.dedupe_hours or 0)
    if hours <= 0 or not history:
        return analyses, []
    recent: list[tuple[str, dict[str, Any]]] = []
    for entry in history:
        raw = str(entry.get("text") or "").strip()
        if not raw:
            continue
        recent.append((qq_digest.normalize_for_match(raw), entry))
    if not recent:
        return analyses, []
    kept: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for item in analyses:
        text = qq_digest.normalize_for_match(item["message"].text)
        hit = None
        for normalized, entry in recent:
            if qq_digest.similar_text(text, normalized):
                hit = entry
                break
        if hit is None:
            kept.append(item)
        else:
            item["suppressed_duplicate"] = {
                "group": str(hit.get("group") or ""),
                "summary": str(hit.get("summary") or ""),
                "ts": str(hit.get("ts") or ""),
            }
            suppressed.append(item)
    return kept, suppressed


def urgent_analyses(
    analyses: Iterable[dict[str, Any]],
    settings: Settings,
    *,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """需要立刻推送的消息：有实质内容 + 强紧急词/待办/48 小时内截止。"""
    if not settings.urgent_immediate:
        return []
    stamp = now or now_local()
    urgent: list[dict[str, Any]] = []
    for item in analyses:
        if is_quiet_group_item(item, settings):
            continue  # 低优先级群只进合并摘要，不触发立即推送
        if item.get("category") != "urgent":
            continue
        score = int(item.get("score") or 0)
        tags = set(item.get("tags") or ())
        if score < 4 or not (tags & SUBSTANTIVE_TAGS):
            continue
        message: Message = item["message"]
        strong = qq_digest._matches_any(message.text, STRONG_URGENT_WORDS)
        deadline = item.get("deadline")
        deadline_soon = bool(
            isinstance(deadline, dt.datetime)
            and dt.timedelta(0) <= (deadline - stamp) <= dt.timedelta(hours=48)
        )
        if strong or item.get("action_words") or deadline_soon:
            urgent.append(item)
    return urgent


def _dedupe_reference(item: dict[str, Any]) -> str:
    """去重时命中的那条历史内容，写进决策日志便于人工核对。"""
    reference = item.get("suppressed_duplicate")
    if not isinstance(reference, dict):
        return ""
    group = str(reference.get("group") or "").strip()
    summary = str(reference.get("summary") or "").strip()[:60]
    return " · ".join(part for part in (group, summary) if part)


def _append_trail(
    trail: list[dict[str, Any]] | None,
    item: dict[str, Any],
    settings: Settings,
    *,
    stage: str,
    outcome: str,
    reason: str,
    dedupe_reason: str = "",
) -> None:
    """记一条决策轨迹；`trail is None` 时什么都不做（预览等只读路径不需要落库）。"""
    if trail is None:
        return
    trail.append(
        {
            "msg_id": str(item.get("msg_id") or ""),
            "group_id": str(item.get("group_id") or ""),
            "stage": stage,
            "outcome": outcome,
            "reason": reason,
            "score": int(item.get("score") or 0),
            "min_score": int(settings.min_score or 0),
            "category": str(item.get("category") or ""),
            "rule_hits": decisions.rule_hits(item),
            "dedupe_reason": dedupe_reason,
        }
    )


def _prepare_items(
    analyses: list[dict[str, Any]],
    settings: Settings,
    history: Iterable[dict[str, Any]] | None = None,
    trail: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], bool, str, bool]:
    candidates: list[dict[str, Any]] = []
    for item in analyses:
        reason = focus_reason(item, settings)
        if reason:
            _append_trail(
                trail,
                item,
                settings,
                stage=decisions.STAGE_FILTER,
                outcome=decisions.FILTERED,
                reason=reason,
            )
            continue
        candidates.append(item)

    if history:
        candidates, suppressed = filter_recent_duplicates(candidates, history, settings)
        if suppressed:
            names = sorted(
                {str(item.get("group_name") or "") for item in suppressed if item.get("group_name")}
            )
            LOGGER.info(
                "跨群去重：%d 条与最近 %d 小时内已推内容重复，不再重复推送（%s）。",
                len(suppressed),
                settings.dedupe_hours,
                "、".join(names) or "来源未知",
            )
            for item in suppressed:
                _append_trail(
                    trail,
                    item,
                    settings,
                    stage=decisions.STAGE_DEDUPE,
                    outcome=decisions.DEDUPED,
                    reason=f"与最近 {int(settings.dedupe_hours)} 小时内已推内容重复",
                    dedupe_reason=_dedupe_reference(item),
                )

    deduped = qq_digest.dedupe_items(candidates)
    kept = {id(item) for item in deduped}
    for item in candidates:
        if id(item) in kept:
            continue
        _append_trail(
            trail,
            item,
            settings,
            stage=decisions.STAGE_DEDUPE,
            outcome=decisions.DEDUPED,
            reason="与本批另一条要点相同",
        )
    candidates = deduped
    candidates.sort(key=lambda value: int(value.get("score") or 0), reverse=True)
    limit = int(settings.max_items)
    for item in candidates[limit:]:
        _append_trail(
            trail,
            item,
            settings,
            stage=decisions.STAGE_FILTER,
            outcome=decisions.TRUNCATED,
            reason=f"命中但超出每批上限 {limit} 条",
        )
    candidates = candidates[:limit]
    for item in candidates:
        _append_trail(
            trail,
            item,
            settings,
            stage=decisions.STAGE_PUBLISH,
            outcome=decisions.PENDING,
            reason=f"命中候选（分值 {int(item.get('score') or 0)}）",
        )

    llm_used = False
    llm_error = ""
    llm_retryable = False
    if settings.llm_enabled and candidates:
        if not settings.dashscope_api_key:
            # 配置缺失属于人为问题：记录原因但不阻塞推送（llm_retryable 保持 False）。
            llm_error = "未配置 DASHSCOPE_API_KEY"
            providers.record_skip(
                purpose=llmstats.PURPOSE_REFINE,
                reason=llm_error,
                model=settings.dashscope_model,
            )
        else:
            # A6 分级路由：先决定这一批值不值得动用模型、动用哪一档，理由随用量一起落库。
            route = routing.decide_route(candidates, settings)
            if route.tier == llmstats.ROUTE_RULE:
                # 本地规则足够：不调模型，但留一条记录说明「这次省了」，省得有账对不上。
                providers.record_skip(
                    purpose=llmstats.PURPOSE_REFINE,
                    reason=route.reason,
                    provider=providers.provider_name(settings),
                    route=route.tier,
                )
            else:
                try:
                    candidates = qq_digest.refine_items(
                        candidates,
                        providers.build_provider(settings, model=route.model),
                        retries=settings.llm_max_retries,
                        backoff=settings.llm_retry_backoff,
                        route=route.tier,
                        route_reason=route.reason,
                    )
                    llm_used = True
                except RuntimeError as error:
                    llm_error = str(error)
                    llm_retryable = llm_should_retry(error)
                    LOGGER.warning("LLM 精炼失败，回退本地规则（%s）：%s", route.label, error)
    return (
        _finalize_items(qq_digest.sort_items(candidates), settings, llm_used=llm_used),
        llm_used,
        llm_error,
        llm_retryable,
    )


EVIDENCE_WORDS = (
    "提交", "报名", "填写", "截止", "材料", "考试", "缴费", "领取",
    "上课", "会议", "讲座", "面试", "答辩", "选课",
)


def evidence_sentence(text: str, item: dict[str, Any] | None = None, limit: int = 300) -> str:
    """挑出最能支撑这条判断的原文句子，供回查用。"""
    value = " ".join(str(text or "").split())
    if not value:
        return ""
    sentences = [part.strip() for part in re.split(r"(?<=[。！？!?；;\n])", value) if part.strip()]
    if not sentences:
        return value[:limit]
    keys: list[str] = []
    deadline = (item or {}).get("deadline_dt") or (item or {}).get("deadline")
    if isinstance(deadline, dt.datetime):
        keys.extend(
            [
                deadline.strftime("%m-%d"),
                deadline.strftime("%m月%d日"),
                f"{deadline.month}月{deadline.day}日",
                f"{deadline.day}日",
                deadline.strftime("%H:%M"),
            ]
        )
    keys.extend(STRONG_URGENT_WORDS)
    keys.extend(EVIDENCE_WORDS)
    for sentence in sentences:
        if any(key and key in sentence for key in keys):
            return sentence[:limit]
    return sentences[0][:limit]


def _finalize_items(
    items: list[dict[str, Any]],
    settings: Settings | None = None,
    *,
    llm_used: bool = False,
) -> list[dict[str, Any]]:
    """补齐摘要/待办/截止/重要度字段，并给出「为什么判为通知」（A7）。"""
    for item in items:
        message: Message = item["message"]
        if not item.get("summary"):
            item["summary"] = qq_digest.local_summary(message.text)
        if not item.get("action"):
            item["action"] = qq_digest.local_action(message.text)
        if not item.get("audience"):
            item["audience"] = ""
        if not item.get("condition"):
            item["condition"] = ""
        if not isinstance(item.get("details"), list):
            item["details"] = []
        if not item.get("deadline_dt"):
            item["deadline_dt"] = item.get("deadline")
        if not item.get("evidence"):
            item["evidence"] = evidence_sentence(message.text, item)
        if not item.get("importance"):
            item["importance"] = IMPORTANCE_BY_CATEGORY.get(str(item.get("category")), 3)
        assessment = confidence.assess_notice(
            item,
            settings,
            authoritative=settings is not None and is_notice_item(item, settings),
            llm_used=llm_used,
        )
        item["confidence"] = confidence.to_payload(assessment)
        item["why"] = item["confidence"]["why"]
    return items


def build_digest(
    settings: Settings,
    records: list[dict[str, Any]],
    *,
    kind: str = "window",
    now: dt.datetime | None = None,
    history: Iterable[dict[str, Any]] | None = None,
) -> Digest:
    stamp = now or now_local()
    analyses = analyse_records(records)
    if kind == "urgent":
        selected = urgent_analyses(analyses, settings)
    else:
        selected = analyses

    trail: list[dict[str, Any]] = []
    items, llm_used, llm_error, llm_retryable = _prepare_items(selected, settings, history, trail)
    groups: list[str] = []
    for record in records:
        name = str(record.get("group_name") or record.get("group_id") or "")
        if name and name not in groups:
            groups.append(name)

    digest = Digest(
        kind=kind,
        window_start=parse_iso(records[0].get("received_at")) if records else stamp,
        window_end=stamp,
        items=items,
        message_ids=[str(record.get("msg_id") or "") for record in records],
        llm_used=llm_used,
        llm_error=llm_error,
        llm_retryable=llm_retryable,
        message_count=len(records),
        groups=groups,
        decisions=trail,
    )
    return finalize_digest(digest, settings)


def _clip(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def format_push_text(digest: Digest, settings: Settings) -> str:
    """生成手机推送文本：只显示精简摘要，默认不附带原文。"""
    stamp = digest.window_end_text or now_local().strftime("%Y-%m-%d %H:%M")
    scope = "、".join(digest.groups) if digest.groups else "未知群"
    lines = [f"【QQ{digest.title}】{stamp}", f"{_clip(scope, 40)} · {digest.message_count} 条消息 · {len(digest.items)} 条重点", ""]

    for index, item in enumerate(digest.items, start=1):
        message: Message = item["message"]
        label = CATEGORY_LABEL.get(str(item.get("category")), "通知")
        summary = item.get("summary") or qq_digest.local_summary(message.text)
        action = item.get("action") or qq_digest.local_action(message.text)
        deadline = qq_digest.format_deadline(item.get("deadline_dt") or item.get("deadline"))
        lines.append(f"{index}. [{label}] {_clip(summary, 60)}")

        details: list[str] = []
        if deadline:
            details.append(f"截止：{deadline}")
        if action and action != summary:
            details.append(f"需做：{_clip(action, 30)}")
        groups = [str(name) for name in (item.get("duplicate_groups") or []) if str(name).strip()]
        if not groups and item.get("group_name"):
            groups = [str(item["group_name"])]
        label = "、".join(dict.fromkeys(groups))
        if len(set(groups)) > 1:
            label = f"{label}（跨群重复）"
        source = " · ".join(part for part in (label, message.sender) if part)
        if source:
            details.append(_clip(source, 45))
        if details:
            lines.append("   " + " · ".join(details))

        links = qq_digest.extract_links(message.text)
        why = str(item.get("why") or "")
        if why:
            note = str((item.get("confidence") or {}).get("text") or "")
            suffix = f"（{note}）" if note else ""
            lines.append(f"   为什么：{_clip(why, 48)}{suffix}")
        if links:
            lines.append(f"   链接：{links[0]}")
        if settings.include_raw:
            original = _clip(message.text, 80)
            if original and original not in summary:
                lines.append(f"   原文：{original}")
        lines.append("")

    if digest.llm_error and digest.items:
        lines.append("（AI 摘要暂不可用，已用本地规则筛选）")
    return "\n".join(lines).strip()


def _deadline_short(value: Any, now: dt.datetime, with_time: bool = True) -> str:
    """把截止时间压成“今天 18:00 / 明天 / 09-30”这种短标签。"""
    if not isinstance(value, dt.datetime):
        return ""
    clock = value.strftime("%H:%M") if with_time else ""
    if value.date() == now.date():
        return f"今天{clock}"
    if value.date() == now.date() + dt.timedelta(days=1):
        return f"明天{clock}"
    return value.strftime("%m-%d") + (f" {clock}" if clock else "")


def push_summary(digest: Digest, limit: int = 26) -> str:
    """通知栏/卡片那一行：最要紧的动作加截止时间，控制在 20 字上下。"""
    if not digest.items:
        return digest.title
    item = digest.items[0]
    message: Message = item["message"]
    summary = str(item.get("summary") or qq_digest.local_summary(message.text))
    deadline_text = qq_digest.format_deadline(item.get("deadline_dt") or item.get("deadline"))
    head = _deadline_short(
        item.get("deadline_dt"),
        digest.window_end or now_local(),
        ":" in str(deadline_text or ""),
    )
    body = _clip(summary, 14)
    text = f"{head} {body}".strip() if head else body
    extra = len(digest.items) - 1
    if extra > 0:
        text = f"{text} +{extra}条"
    return _clip(text, limit)


def html_push_text(digest: Digest, settings: Settings) -> str:
    """WxPusher HTML 正文：要动手的做成色块，只需知悉的压成一行。"""
    stamp = digest.window_end_text or now_local().strftime("%Y-%m-%d %H:%M")
    scope = "、".join(digest.groups) if digest.groups else "未知群"
    parts: list[str] = [
        '<div style="font-size:12px;color:#9aa0a6">'
        f"{html_escape(stamp)} · {html_escape(_clip(scope, 30))} · {digest.message_count} 条消息</div>"
    ]
    actions = [item for item in digest.items if str(item.get("category")) in {"urgent", "action"}]
    others = [item for item in digest.items if str(item.get("category")) not in {"urgent", "action"}]

    for item in actions[:5]:
        message: Message = item["message"]
        urgent = str(item.get("category")) == "urgent"
        color = "#d93025" if urgent else "#f29900"
        label = "紧急" if urgent else "待办"
        deadline_text = qq_digest.format_deadline(item.get("deadline_dt") or item.get("deadline"))
        summary = str(item.get("summary") or qq_digest.local_summary(message.text))
        action = str(item.get("action") or qq_digest.local_action(message.text))
        evidence = str(item.get("evidence") or "")
        head = " · ".join(
            part for part in ((f"{deadline_text} 截止" if deadline_text else ""), summary) if part
        )
        groups = "、".join(str(name) for name in (item.get("duplicate_groups") or []))
        meta = " · ".join(
            part for part in (groups or str(item.get("group_name") or ""), message.sender) if part
        )
        block = [
            '<div style="margin:10px 0;padding:10px 12px;background:#fff8ec;'
            f'border-left:3px solid {color};border-radius:3px">',
            f'<div style="font-size:12px;color:{color};font-weight:600">{label}</div>',
            '<div style="font-size:15px;font-weight:600;color:#1a1a1a;margin-top:2px">'
            f"{html_escape(_clip(head, 46))}</div>",
        ]
        if action and action != summary:
            block.append(
                '<div style="font-size:13px;color:#333;margin-top:4px">'
                f"需做：{html_escape(_clip(action, 30))}</div>"
            )
        if meta:
            block.append(
                '<div style="font-size:12px;color:#9aa0a6;margin-top:4px">'
                f"{html_escape(_clip(meta, 40))}</div>"
            )
        audience = str(item.get("audience") or "")
        condition = str(item.get("condition") or "")
        if audience or condition:
            context_text = " · ".join(
                part for part in (
                    f"适用：{audience}" if audience else "",
                    f"条件：{condition}" if condition else "",
                ) if part
            )
            block.append(
                '<div style="font-size:12px;color:#5f6368;margin-top:3px">'
                f"{html_escape(_clip(context_text, 90))}</div>"
            )
        why = str(item.get("why") or "")
        if why:
            note = str((item.get("confidence") or {}).get("text") or "")
            suffix = f"（{note}）" if note else ""
            block.append(
                '<div style="font-size:11px;color:#8a8f98;margin-top:4px">'
                f"为什么：{html_escape(_clip(why, 60))}{html_escape(suffix)}</div>"
            )
        if evidence and evidence not in summary:
            block.append(
                '<div style="font-size:12px;color:#b0b0b0;margin-top:2px">'
                f"原文：{html_escape(_clip(evidence, 40))}</div>"
            )
        links = qq_digest.extract_links(message.text)
        task_url = str(item.get("task_url") or "")
        if task_url:
            block.append(
                f'<div style="font-size:12px;margin-top:5px"><a href="{html_escape(task_url)}" '
                'style="color:#1a73e8">查看完整原文</a></div>'
            )
        if links:
            block.append(
                f'<div style="font-size:12px;margin-top:4px"><a href="{html_escape(links[0])}" '
                'style="color:#1a73e8">相关链接</a></div>'
            )
        block.append("</div>")
        parts.append("".join(block))

    if others:
        names = " · ".join(
            html_escape(
                _clip(str(item.get("summary") or qq_digest.local_summary(item["message"].text)), 22)
            )
            for item in others[:5]
        )
        more = f" 等 {len(others)} 条" if len(others) > 5 else ""
        parts.append(
            '<div style="font-size:13px;color:#5f6368;margin-top:12px">'
            f"其余 {len(others)} 条：{names}{more}</div>"
        )
    if digest.llm_error and digest.items:
        parts.append(
            '<div style="font-size:12px;color:#9aa0a6;margin-top:8px">'
            "（AI 摘要暂不可用，已用本地规则筛选）</div>"
        )
    return "".join(parts)


def finalize_digest(digest: Digest, settings: Settings) -> Digest:
    """补齐三种渲染：存档纯文本、通知栏一句话、HTML 卡片。"""
    if not digest.items:
        return digest
    digest.body = format_push_text(digest, settings)
    digest.summary = push_summary(digest)
    digest.html = html_push_text(digest, settings) if settings.html_push else ""
    return digest


def preview_text(settings: Settings, records: list[dict[str, Any]]) -> str:
    """不发送，仅生成预览文本。"""
    if not records:
        return "没有待处理消息。"
    digest = build_digest(settings, records, kind="window")
    if not digest.items:
        return f"待处理 {len(records)} 条消息，未筛出重点内容（不会推送）。"
    header = f"[仅预览] 待处理 {len(records)} 条，命中 {len(digest.items)} 条\n\n"
    return header + digest.body
