"""决策日志词表：把「为什么推 / 为什么不推」统一成可查询的结构化记录（Roadmap A33）。

本模块只定义**词表与归一化**，不碰数据库、不碰业务判断的分支逻辑：

- `store.py` 负责落库与查询；
- `summarizer.py` 在筛选 / 去重 / 截断时给出原因；
- `service.py` 负责入口拒绝与投递结果的回填。

一条消息在一次处理里最多留下一条最终结论，按 `msg_id` 就能把它读成一段决策轨迹。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

# ------------------------------------------------------------------ 结论词表
PENDING = "pending"      # 已命中候选但还没投递，最终结论由投递阶段回填
PUSHED = "pushed"        # 进了摘要并且确实推送到至少一个通道
HELD = "held"            # 进了摘要但本次没投出去（暂无通道 / 全部失败 / 稍后重试）
FILTERED = "filtered"    # 被本地规则判定不值得处理
DEDUPED = "deduped"      # 与最近已推内容或同批另一条重复
TRUNCATED = "truncated"  # 命中但超出每批条数上限
DUPLICATE = "duplicate"  # msg_id 重复，未入库
REJECTED = "rejected"    # 入口拒绝：没有 msg_id，或群不在白名单
DEFERRED = "deferred"    # 本该推送，但被夜间静默 / 当日额度 / 大模型失败推迟，还没有最终结论

OUTCOMES = (PENDING, PUSHED, HELD, DEFERRED, FILTERED, DEDUPED, TRUNCATED, DUPLICATE, REJECTED)

# 「为什么没推」——查询时最常问的几个
NEGATIVE_OUTCOMES = (FILTERED, DEDUPED, TRUNCATED, REJECTED, DUPLICATE)

# 「还没落定」——这些行是暂态，后面会被同一批 / 下一次 tick 覆盖成最终结论
UNDECIDED_OUTCOMES = (PENDING, DEFERRED)

OUTCOME_LABELS = {
    PENDING: "待投递",
    PUSHED: "已推送",
    HELD: "未投出",
    DEFERRED: "延后未决",
    FILTERED: "未命中",
    DEDUPED: "重复跳过",
    TRUNCATED: "超出上限",
    DUPLICATE: "重复消息",
    REJECTED: "入口拒绝",
}

# 决策点：入口 → 筛选 → 去重 → 投递
STAGE_INTAKE = "intake"
STAGE_FILTER = "filter"
STAGE_DEDUPE = "dedupe"
STAGE_PUBLISH = "publish"
STAGES = (STAGE_INTAKE, STAGE_FILTER, STAGE_DEDUPE, STAGE_PUBLISH)

# (分析结果里的字段, 展示用标签)
_RULE_FIELDS = (
    ("urgent", "紧急词"),
    ("action_words", "行动词"),
    ("academic_words", "学术词"),
    ("admin_words", "管理词"),
)


def rule_hits(analysis: Mapping[str, Any], *, limit: int = 3) -> list[str]:
    """把一次分析结果压成「命中了什么」的短列表，用于人读与回归对比。"""
    hits = [str(tag) for tag in sorted(str(tag) for tag in (analysis.get("tags") or ()) if str(tag).strip())]
    for key, label in _RULE_FIELDS:
        words = [str(word) for word in (analysis.get(key) or ()) if str(word).strip()]
        if words:
            hits.append(f"{label}:{'/'.join(words[:limit])}")
    deadline = analysis.get("deadline")
    if deadline is not None:
        hits.append(f"截止:{deadline}")
    return hits


def outcome_label(outcome: str) -> str:
    value = str(outcome or "")
    return OUTCOME_LABELS.get(value, value or "未知")


def is_final(outcome: str) -> bool:
    """这条结论算不算最终决定？「待投递 / 延后未决」只是过程态，不算。"""
    value = str(outcome or "")
    return value in OUTCOMES and value not in UNDECIDED_OUTCOMES


def split_counts(counts: Mapping[str, int]) -> tuple[dict[str, int], dict[str, int]]:
    """把 outcome→条数 拆成（已决, 未决）两组，供 CLI 分开显示。"""
    final: dict[str, int] = {}
    undecided: dict[str, int] = {}
    for key, value in counts.items():
        bucket = undecided if str(key) in UNDECIDED_OUTCOMES else final
        bucket[str(key)] = int(value)
    return final, undecided


def summarise_counts(counts: Mapping[str, int]) -> str:
    """把 outcome→条数 渲染成一行摘要，按条数从多到少。"""
    parts = [f"{outcome_label(key)} {int(value)}" for key, value in sorted(counts.items(), key=lambda kv: -int(kv[1]))]
    return "、".join(parts)


def describe_rows(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """把决策行渲染成给人看的短句，供 CLI 复用。"""
    lines: list[str] = []
    for row in rows:
        stamp = str(row.get("created_at") or "").replace("T", " ")[:19]
        label = outcome_label(str(row.get("outcome") or ""))
        lines.append(f"{stamp}  [{label}] {row.get('reason') or '（未记录原因）'}")
        bits: list[str] = []
        if row.get("score") or row.get("min_score"):
            bits.append(f"分值 {int(row.get('score') or 0)}/{int(row.get('min_score') or 0)}")
        if row.get("category"):
            bits.append(str(row["category"]))
        if row.get("msg_id"):
            bits.append(f"msg {row['msg_id']}")
        if int(row.get("digest_id") or 0):
            bits.append(f"摘要 #{int(row['digest_id'])}")
        if bits:
            lines.append("    " + " · ".join(bits))
        hits = row.get("rule_hits") or []
        if isinstance(hits, str):
            hits = json.loads(hits) if hits.strip().startswith("[") else []
        hits = [str(hit) for hit in hits if str(hit).strip()]
        if hits:
            lines.append("    命中：" + "、".join(hits))
        dedupe = str(row.get("dedupe_reason") or "").strip()
        if dedupe:
            lines.append(f"    对照：{dedupe}")
    return lines
