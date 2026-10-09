"""置信度与「为什么」（Roadmap A7）。

一句话：**每条候选都要能回答「凭什么判它值得推 / 值得办」**——给一个 0–1 的
`confidence`、一组人话化的触发规则（`triggers`）和一组机器可读信号（`signals`）。

这里是纯函数：不碰数据库、不碰网络、不调用模型。所以推送、待办台、`main.py show`
和回归测试看到的是同一套解释，不会各算各的。

两个尺度是**故意分开**的，回答两个不同问题：

- `assess_notice`：「这条消息算不算值得看的通知」——摘要候选用；
- `assess_task`：「这条算不算一个能直接执行的待办」——待办分类用（权重沿用 A8，
  这里只是把每一步都变成可解释的触发规则，数值不变）。

权重都写在触发规则里（`+0.18 分值 6，高出阈值 3 两分以上`），所以解释和算分永远不会脱节。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .decisions import word_hits

# ------------------------------------------------------------------ 等级词表
LEVEL_HIGH = "high"
LEVEL_MEDIUM = "medium"
LEVEL_LOW = "low"

LEVELS = (LEVEL_HIGH, LEVEL_MEDIUM, LEVEL_LOW)

LEVEL_LABELS = {LEVEL_HIGH: "高", LEVEL_MEDIUM: "中", LEVEL_LOW: "低"}

#: 等级阈值：≥ high 直接可用；< low 建议人工确认（与 `QQ_DIGEST_CANDIDATE_MIN_CONFIDENCE` 默认值一致）。
DEFAULT_HIGH = 0.75
DEFAULT_LOW = 0.55

# ------------------------------------------------------------------ 信号词表
SIG_SCORE_MARGIN = "score_margin"      # 分值高出阈值
SIG_WORD_HIT = "word_hit"              # 撞上紧急/行动/学术/管理词表
SIG_DEADLINE = "deadline"              # 识别到截止时间
SIG_URGENT = "urgent"                  # 规则判为紧急
SIG_ACTION = "action"                  # 有可执行动作
SIG_DIRECTIVE = "directive"            # 原话是明确指令（务必/必须/请）
SIG_NOTICE_GROUP = "notice_group"      # 来自通知类群
SIG_EVIDENCE = "evidence"              # 能摘出支撑判断的原文句
SIG_LLM = "llm_refined"                # 经过大模型精炼
SIG_WEAK_WORDING = "weak_wording"      # 原话有「尽量/也许」等弱措辞
SIG_AMBIGUOUS_TIME = "ambiguous_time"  # 时间语义含糊（近期/尽快）
SIG_QUIET_GROUP = "quiet_group"        # 安静群，降噪处理
SIG_NO_ACTION = "no_action"            # 没识别到行动

# ------------------------------------------------------------------ 词表（与 A8 一致）
WEAK_ACTION_WORDS = ("记得", "别忘了", "不要忘记", "可能要", "可能", "也许", "大概", "尽量", "有空", "考虑", "可以在")
STRONG_DIRECTIVE_WORDS = (
    "请", "务必", "必须", "要求", "按时", "尽早", "截止", "前完成", "前提交",
)
AMBIGUOUS_TIME_WORDS = ("近期", "近日", "稍后", "左右", "前后", "之前要", "尽快")
ACTION_VERB_WORDS = (
    "提交", "填写", "报名", "缴费", "领取", "参加", "完成", "上传", "确认",
    "回复", "核对", "下载", "安装", "办理", "注册", "认证", "上交",
)


@dataclass(frozen=True)
class Assessment:
    """一次评估的结果：分数 + 等级 + 为什么。"""

    confidence: float
    level: str
    triggers: tuple[str, ...] = ()
    signals: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def reason(self) -> str:
        """一行话的「为什么」；没有触发规则时如实说明。"""
        return describe_triggers(self.triggers) or "没有命中任何规则"


# ------------------------------------------------------------------ 小工具
def _clamp(value: float) -> float:
    return round(max(0.05, min(0.98, float(value))), 2)


def level_of(confidence: float, *, high: float = DEFAULT_HIGH, low: float = DEFAULT_LOW) -> str:
    value = float(confidence or 0.0)
    if value >= float(high or DEFAULT_HIGH):
        return LEVEL_HIGH
    if value >= float(low or DEFAULT_LOW):
        return LEVEL_MEDIUM
    return LEVEL_LOW


def level_label(level: str) -> str:
    return LEVEL_LABELS.get(str(level or ""), str(level or ""))


def percent(confidence: float) -> int:
    return int(round(float(confidence or 0.0) * 100))


def confidence_text(confidence: float, *, high: float = DEFAULT_HIGH, low: float = DEFAULT_LOW) -> str:
    """给人看的一句话，例如「把握较高 82%」（待办台沿用这套话术）。"""
    words = {LEVEL_HIGH: "把握较高", LEVEL_MEDIUM: "把握中等", LEVEL_LOW: "把握较低"}
    level = level_of(confidence, high=high, low=low)
    return f"{words[level]} {percent(confidence)}%"


def describe_triggers(triggers: Sequence[Any], *, limit: int = 4) -> str:
    """把触发规则拼成一行人话（超出就省略）。"""
    values = [str(item) for item in triggers if str(item or "").strip()]
    if not values:
        return ""
    shown = values[: max(1, int(limit))]
    text = "；".join(shown)
    if len(values) > len(shown):
        text += f"；另有 {len(values) - len(shown)} 条"
    return text


def to_payload(assessment: Assessment, *, limit: int = 4) -> dict[str, Any]:
    """把一次评估压成可 JSON 归档的形状（推送、待办台、`main.py show` 共用）。"""
    return {
        "score": assessment.confidence,
        "level": assessment.level,
        "text": confidence_text(assessment.confidence),
        "triggers": list(assessment.triggers),
        "signals": list(assessment.signals),
        "why": describe_triggers(assessment.triggers, limit=limit),
    }


def _deadline_text(moment: Any) -> str:
    if isinstance(moment, dt.datetime):
        if (moment.hour, moment.minute) == (0, 0):
            return moment.strftime("%Y-%m-%d")
        return moment.strftime("%Y-%m-%d %H:%M")
    text = str(moment or "").strip()
    return text


# ------------------------------------------------------------------ 通知置信度
def assess_notice(
    item: Mapping[str, Any],
    settings: Any = None,
    *,
    authoritative: bool = False,
    llm_used: bool = False,
) -> Assessment:
    """这条消息算不算「值得看的通知」？返回分数 + 触发规则。

    权重（从 0.30 起算）：
    `分值高出阈值 ≥2 → +0.18 / =1 → +0.10`、`命中词表 +0.12`、`有截止时间 +0.18`、
    `规则判紧急 +0.12`、`有行动 +0.10`、`通知类群 +0.06`、`有原文依据 +0.06`、
    `AI 精炼过 +0.04`；`安静群 -0.10`、`既没截止也没行动也不紧急 -0.08`。
    """
    score = int(item.get("score") or 0)
    min_score = int(getattr(settings, "min_score", 0) or 0)
    moment = item.get("deadline_dt") or item.get("deadline")
    words = word_hits(item)
    tags = {str(tag) for tag in (item.get("tags") or ()) if str(tag).strip()}
    evidence = str(item.get("evidence") or "").strip()
    action = str(item.get("action") or "").strip()
    quiet = bool(item.get("quiet_group"))
    urgent = bool(item.get("urgent")) or "urgent" in tags

    triggers: list[str] = []
    signals: list[str] = []
    confidence = 0.30

    margin = score - min_score
    if min_score and margin >= 2:
        confidence += 0.18
        triggers.append(f"+0.18 分值 {score}，高出阈值 {min_score} 两分以上")
        signals.append(SIG_SCORE_MARGIN)
    elif min_score and margin >= 1:
        confidence += 0.10
        triggers.append(f"+0.10 分值 {score}，刚过阈值 {min_score}")
        signals.append(SIG_SCORE_MARGIN)
    else:
        triggers.append(f"+0.00 分值 {score}（阈值 {min_score or '未设'}）")

    if words:
        confidence += 0.12
        triggers.append("+0.12 命中" + "、".join(words))
        signals.append(SIG_WORD_HIT)
    if moment is not None:
        confidence += 0.18
        triggers.append(f"+0.18 识别到截止时间 {_deadline_text(moment)}")
        signals.append(SIG_DEADLINE)
    if urgent:
        confidence += 0.12
        triggers.append("+0.12 规则判为紧急")
        signals.append(SIG_URGENT)
    if action:
        confidence += 0.10
        triggers.append(f"+0.10 有明确行动：{action[:20]}")
        signals.append(SIG_ACTION)
    if authoritative:
        confidence += 0.06
        triggers.append("+0.06 来自通知类群")
        signals.append(SIG_NOTICE_GROUP)
    if evidence:
        confidence += 0.06
        triggers.append("+0.06 能摘出支撑判断的原文句")
        signals.append(SIG_EVIDENCE)
    if llm_used:
        confidence += 0.04
        triggers.append("+0.04 经过大模型精炼")
        signals.append(SIG_LLM)
    if quiet:
        confidence -= 0.10
        triggers.append("-0.10 安静群，按降噪处理")
        signals.append(SIG_QUIET_GROUP)
    if moment is None and not action and not urgent:
        confidence -= 0.08
        triggers.append("-0.08 既没截止时间也没有行动，像信息性广播")
        signals.append(SIG_NO_ACTION)

    return Assessment(
        confidence=_clamp(confidence),
        level=level_of(confidence),
        triggers=tuple(triggers),
        signals=tuple(signals),
        details={
            "score": score,
            "min_score": min_score,
            "words": tuple(words),
            "has_deadline": moment is not None,
            "quiet": quiet,
        },
    )



# ------------------------------------------------------------------ 待办置信度
def assess_task(
    item: Mapping[str, Any],
    *,
    quiet: bool = False,
    evidence: str | None = None,
    high: float = DEFAULT_HIGH,
    low: float = DEFAULT_LOW,
) -> Assessment:
    """这条算不算「能直接执行的待办」？权重与 A8 完全一致，只是把每步都写进触发规则。

    `high` / `low` 是等级阈值；调用方把 `low` 传成 `QQ_DIGEST_CANDIDATE_MIN_CONFIDENCE`，
    低于它的候选一律先交人工确认（A8）。
    """
    message = item.get("message")
    text = str(getattr(message, "text", "") or "")
    category = str(item.get("category") or "info")
    action_words = [str(word) for word in (item.get("action_words") or ()) if str(word)]
    deadline = item.get("deadline_dt") or item.get("deadline")
    has_deadline = isinstance(deadline, dt.datetime)
    if evidence is None:
        evidence = str(item.get("evidence") or "")
    evidence = str(evidence).strip()
    score = int(item.get("score") or 0)
    weak = next((word for word in WEAK_ACTION_WORDS if word in text), "")
    ambiguous = next((word for word in AMBIGUOUS_TIME_WORDS if word in text), "")
    strong_directive = any(word in text for word in STRONG_DIRECTIVE_WORDS)
    direct_action = bool(action_words) or any(word in text for word in ACTION_VERB_WORDS)
    urgent = category == "urgent"

    triggers: list[str] = ["+0.28 基础分：像一条待办"]
    signals: list[str] = []
    confidence = 0.28

    if has_deadline:
        confidence += 0.25
        triggers.append(f"+0.25 有明确截止时间 {_deadline_text(deadline)}")
        signals.append(SIG_DEADLINE)
    if strong_directive:
        confidence += 0.20
        triggers.append("+0.20 原话是明确指令（请 / 务必 / 必须 / 截止）")
        signals.append(SIG_DIRECTIVE)
    if direct_action:
        confidence += 0.14
        detail = "、".join(action_words[:3]) or "动词"
        triggers.append(f"+0.14 有可执行动作：{detail}")
        signals.append(SIG_ACTION)
    if urgent:
        confidence += 0.10
        triggers.append("+0.10 规则判为紧急")
        signals.append(SIG_URGENT)
    if category == "action":
        confidence += 0.08
        triggers.append("+0.08 归类为行动项")
    if evidence:
        confidence += 0.05
        triggers.append("+0.05 能摘出支撑判断的原文句")
        signals.append(SIG_EVIDENCE)
    if score >= 4:
        confidence += 0.05
        triggers.append(f"+0.05 规则分值较高（{score}）")
        signals.append(SIG_SCORE_MARGIN)
    if weak:
        confidence -= 0.24
        triggers.append(f"-0.24 原话有“{weak}”等不确定措辞")
        signals.append(SIG_WEAK_WORDING)
    if ambiguous:
        confidence -= 0.14
        triggers.append(f"-0.14 “{ambiguous}”的时间语义不明确")
        signals.append(SIG_AMBIGUOUS_TIME)
    if quiet:
        confidence -= 0.08
        triggers.append("-0.08 安静群，按降噪处理")
        signals.append(SIG_QUIET_GROUP)
    if not direct_action:
        confidence -= 0.08
        triggers.append("-0.08 没识别到具体要做什么")
        signals.append(SIG_NO_ACTION)

    return Assessment(
        confidence=_clamp(confidence),
        level=level_of(confidence, high=high, low=low),
        triggers=tuple(triggers),
        signals=tuple(signals),
        details={
            "has_deadline": has_deadline,
            "weekly_weak_word": weak,
            "ambiguous_word": ambiguous,
            "quiet": quiet,
            "confirm_threshold": float(low),
        },
    )
