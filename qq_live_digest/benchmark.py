"""脱敏评测集与可复现指标（Roadmap A21）。

评测集由 A20 模拟器自带：``simulator.generate()`` 给每条消息附一个 ``_expect``
块（意图标注：该不该推 / 该不该建待办 / 文本里有没有截止时间）。本模块把它当
ground truth，跑一遍**真实链路**的回放，再从库里把指标算出来：

- **召回 / 误报**：该推的推出去了几条；闲聊、广告这类噪声有没有被误推；
- **待办判定**：该建待办的是否都进了待办库；
- **截止时间**：文本里承诺的 ``{when}`` 是否解析成了结构化 deadline；
- **跨群去重**：同一条通知在同一个窗口里被转发了两次，是否只留一条；
- **延迟**：消息到达 → 它所在那次推送的窗口结束，中间隔了多久；
- **成本**：这轮回放一共发生多少次模型调用、多少 token、折合多少钱；
- **置信度**：三档各多少条（A7 的解释性指标）。

全程离线：内存假通道、不调大模型、临时目录，同 seed 必得同一组数字。

设计取舍：

1. 评测集不另建数据集，直接把模拟器的意图标注当 ground truth——数据是编造的、
   标注是生成时确定写进记录的，所以指标可离线复现、可进 CI；
2. 「召回」按**认出来了**算（推出 / 被去重 / 超上限都算命中），另外单独报
   「送达率」，避免把窗口合并与去重这些正常行为误判成漏召；
3. 「误报」只看明确该挡的噪声（闲聊 / 课程闲聊 / 广告）有没有被推出去；
4. 「跨群去重」不靠标注，而按「同一内容在同一窗口里第二次出现、且第一次是可推
   正类」自行推出——与生成器里重复转发的实现细节解耦；
5. 待办 / 截止时间的分母只算**真进了摘要**的候选，否则被去重掉的那条会凭空
   拉低命中率。
"""

from __future__ import annotations

import datetime as dt
import logging
import statistics
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import confidence as confidence_mod
from . import decisions as decisions_mod
from . import llmstats
from .simulator import build_service, generate, read_fixture, replay, write_fixture
from .timeutil import parse_iso

LOGGER = logging.getLogger(__name__)

DEFAULT_COUNT = 500
DEFAULT_SEED = 20261008

# 「认出来了」：推出去，或因重复 / 超上限被挡在摘要门口——都算召回命中
DETECTED_OUTCOMES = frozenset(
    {decisions_mod.PUSHED, decisions_mod.HELD, decisions_mod.DEDUPED, decisions_mod.TRUNCATED}
)
# 「真被当成要点推送」：用于算送达率与误报
DELIVERED_OUTCOMES = frozenset({decisions_mod.PUSHED, decisions_mod.HELD})


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _percentile(values: list[float], percent: float) -> float:
    """最近秩百分位（不插值）：样本少的时候比插值更稳。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, int(round(percent / 100.0 * len(ordered) + 0.5)) - 1)
    return float(ordered[min(rank, len(ordered) - 1)])


@dataclass
class BenchmarkReport:
    """一次评测的可复现结果（字段全部可 JSON 序列化）。"""

    messages: int = 0
    seed: int = 0
    # 召回 / 误报
    positives: int = 0
    detected_positives: int = 0
    delivered_positives: int = 0
    negatives: int = 0
    false_positives: int = 0
    # 待办
    todo_expected: int = 0
    todo_created: int = 0
    tasks_total: int = 0
    # 截止时间
    deadline_expected: int = 0
    deadline_parsed: int = 0
    # 跨群去重
    dedupe_expected: int = 0
    dedupe_hit: int = 0
    # 漏斗与延迟
    funnel: dict[str, int] = field(default_factory=dict)
    latency_samples: int = 0
    latency_median_min: float = 0.0
    latency_p95_min: float = 0.0
    # 成本
    llm_calls: int = 0
    llm_failed: int = 0
    llm_retried: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    price_in: float = 0.0
    price_out: float = 0.0
    cost_yuan: float = 0.0
    # 解释性
    confidence: dict[str, int] = field(default_factory=dict)
    per_kind: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def recall(self) -> float:
        return 0.0 if not self.positives else self.detected_positives / self.positives

    @property
    def delivery_rate(self) -> float:
        return 0.0 if not self.positives else self.delivered_positives / self.positives

    @property
    def false_positive_rate(self) -> float:
        return 0.0 if not self.negatives else self.false_positives / self.negatives

    @property
    def todo_rate(self) -> float:
        return 0.0 if not self.todo_expected else self.todo_created / self.todo_expected

    @property
    def deadline_rate(self) -> float:
        return 0.0 if not self.deadline_expected else self.deadline_parsed / self.deadline_expected

    @property
    def dedupe_rate(self) -> float:
        return 0.0 if not self.dedupe_expected else self.dedupe_hit / self.dedupe_expected

    def as_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data.update(
            {
                "recall": round(self.recall, 4),
                "delivery_rate": round(self.delivery_rate, 4),
                "false_positive_rate": round(self.false_positive_rate, 4),
                "todo_rate": round(self.todo_rate, 4),
                "deadline_rate": round(self.deadline_rate, 4),
                "dedupe_rate": round(self.dedupe_rate, 4),
            }
        )
        return data

    def summary_lines(self) -> list[str]:
        lines = [
            f"评测集：{self.messages} 条消息 / seed={self.seed}",
            f"召回：{self.detected_positives}/{self.positives}（{_pct(self.recall)}）"
            f" · 其中真送达 {self.delivered_positives} 条（{_pct(self.delivery_rate)}）",
            f"误报：{self.false_positives}/{self.negatives} 条噪声被推出去"
            f"（{_pct(self.false_positive_rate)}）",
            f"待办：{self.todo_created}/{self.todo_expected}（{_pct(self.todo_rate)}）"
            f" · 待办库共 {self.tasks_total} 项",
            f"截止时间：{self.deadline_parsed}/{self.deadline_expected}（{_pct(self.deadline_rate)}）",
            f"跨群去重：{self.dedupe_hit}/{self.dedupe_expected}（{_pct(self.dedupe_rate)}）",
        ]
        if self.latency_samples:
            lines.append(
                f"延迟：中位 {self.latency_median_min:.1f} 分钟 · P95 {self.latency_p95_min:.1f} 分钟"
                f"（{self.latency_samples} 条要点）"
            )
        cost_bits = [f"模型调用 {self.llm_calls} 次"]
        if self.llm_failed:
            cost_bits.append(f"失败 {self.llm_failed} 次")
        if self.llm_retried:
            cost_bits.append(f"重试 {self.llm_retried} 次")
        cost_bits.append(f"token {self.prompt_tokens}+{self.completion_tokens}")
        if self.price_in or self.price_out:
            cost_bits.append(f"≈ {llmstats.format_cost(self.cost_yuan)}")
        else:
            cost_bits.append("未配单价，费用按 0 记")
        lines.append("成本：" + " · ".join(cost_bits))
        if self.confidence:
            labels = []
            for level in confidence_mod.LEVELS:
                if level in self.confidence:
                    labels.append(f"{confidence_mod.level_label(level)} {self.confidence[level]}")
            extra = [key for key in self.confidence if key not in confidence_mod.LEVELS]
            for key in sorted(extra):
                labels.append(f"{key or '未标注'} {self.confidence[key]}")
            lines.append("置信度：" + "、".join(labels))
        return lines


def _outcomes_by_message(store: Any, records: list[dict[str, Any]]) -> dict[str, set[str]]:
    limit = max(50, len(records) * 8)
    outcomes: dict[str, set[str]] = {}
    for row in store.recent_decisions(limit=limit):
        outcomes.setdefault(str(row.get("msg_id") or ""), set()).add(str(row.get("outcome") or ""))
    return outcomes


def _duplicate_expectations(
    records: list[dict[str, Any]], window_minutes: int
) -> set[str]:
    """同一内容在同一窗口里第二次出现、且第一次是可推正类的消息 id。"""
    window = dt.timedelta(minutes=max(1, int(window_minutes)))
    first_positive: dict[str, dt.datetime] = {}
    expected: set[str] = set()
    ordered = sorted(records, key=lambda item: str(item.get("received_at") or ""))
    for record in ordered:
        content = str(record.get("content") or "")
        moment = parse_iso(record.get("received_at"))
        if not content or not isinstance(moment, dt.datetime):
            continue
        first = first_positive.get(content)
        if isinstance(first, dt.datetime) and dt.timedelta(0) < (moment - first) <= window:
            expected.add(str(record.get("msg_id") or ""))
        if (record.get("_expect") or {}).get("push") and content not in first_positive:
            first_positive[content] = moment
    return expected


def evaluate(
    records: list[dict[str, Any]],
    *,
    data_dir: Path | str,
    quiet_hours: str = "",
    window_minutes: int = 30,
    daily_budget: int = 0,
    max_items: int = 30,
    min_score: int = 3,
    step_minutes: int = 5,
    seed: int = DEFAULT_SEED,
    price_in: float = 0.0,
    price_out: float = 0.0,
    logger: logging.Logger | None = None,
) -> BenchmarkReport:
    """把评测集喂进真实链路，返回一份可复现的指标报告。"""
    for record in records:
        if not isinstance(record.get("_expect"), dict):
            raise ValueError(
                "评测集缺少 _expect 意图标注；请用 simulator.generate() 重新生成"
                "（旧 fixture 需先重跑 --out 再评测）"
            )
    service, channel = build_service(
        data_dir,
        quiet_hours=quiet_hours,
        window_minutes=window_minutes,
        daily_budget=daily_budget,
        max_items=max_items,
        min_score=min_score,
        logger=logger,
    )
    funnel = replay(
        records,
        data_dir=data_dir,
        quiet_hours=quiet_hours,
        window_minutes=window_minutes,
        daily_budget=daily_budget,
        max_items=max_items,
        min_score=min_score,
        step_minutes=step_minutes,
        service=service,
        channel=channel,
        logger=logger,
    )
    store = service.store

    outcomes = _outcomes_by_message(store, records)
    task_keys = {str(row.get("task_key") or "") for row in store.list_tasks(limit=max(1, len(records) * 2))}

    # 摘要里真出现过的要点：算截止时间 / 待办 / 延迟 / 置信度都以它为准
    in_digest: set[str] = set()
    deadline_parsed_ids: set[str] = set()
    received = {str(item.get("msg_id") or ""): parse_iso(item.get("received_at")) for item in records}
    latencies: list[float] = []
    confidence_counts: dict[str, int] = {}
    for digest in store.recent_digests(limit=max(1, len(records))):
        window_end = parse_iso(digest.get("window_end"))
        for item in digest.get("items") or []:
            msg_id = str(item.get("msg_id") or "")
            if not msg_id:
                continue
            in_digest.add(msg_id)
            if str(item.get("deadline") or "").strip():
                deadline_parsed_ids.add(msg_id)
            level = str((item.get("confidence") or {}).get("level") or "")
            confidence_counts[level] = confidence_counts.get(level, 0) + 1
            start = received.get(msg_id)
            if isinstance(start, dt.datetime) and isinstance(window_end, dt.datetime):
                # 回放的调度时钟走固定步长，批量 tick 的时间戳可能比它刚吃进去的消息早
                # 不到一步；这类「到达即推」按 0 记，不出现负延迟。
                latencies.append(max(0.0, (window_end - start).total_seconds() / 60.0))

    dedupe_expected_ids = _duplicate_expectations(records, window_minutes)
    report = BenchmarkReport(
        messages=len(records),
        seed=int(seed),
        tasks_total=int(store.task_stats().get("total") or 0),
        funnel=dict(funnel.decisions),
        latency_samples=len(latencies),
        latency_median_min=float(statistics.median(latencies)) if latencies else 0.0,
        latency_p95_min=_percentile(latencies, 95),
        price_in=float(price_in or 0.0),
        price_out=float(price_out or 0.0),
        confidence=confidence_counts,
    )
    report.dedupe_expected = len(dedupe_expected_ids)
    report.dedupe_hit = sum(
        1 for msg_id in dedupe_expected_ids if decisions_mod.DEDUPED in outcomes.get(msg_id, set())
    )

    per_kind: dict[str, dict[str, int]] = {}
    for record in records:
        expected = record.get("_expect") or {}
        kind = str(expected.get("kind") or "unknown")
        msg_id = str(record.get("msg_id") or "")
        got = outcomes.get(msg_id, set())
        bucket = per_kind.setdefault(
            kind, {"total": 0, "detected": 0, "delivered": 0, "task": 0, "deadline": 0}
        )
        bucket["total"] += 1
        if got & DETECTED_OUTCOMES:
            bucket["detected"] += 1
        if got & DELIVERED_OUTCOMES:
            bucket["delivered"] += 1
        if msg_id in task_keys:
            bucket["task"] += 1
        if msg_id in deadline_parsed_ids:
            bucket["deadline"] += 1

        if expected.get("push") is True:
            report.positives += 1
            if got & DETECTED_OUTCOMES:
                report.detected_positives += 1
            if got & DELIVERED_OUTCOMES:
                report.delivered_positives += 1
        elif expected.get("push") is False:
            report.negatives += 1
            if got & DELIVERED_OUTCOMES:
                report.false_positives += 1
        if expected.get("todo") and msg_id in in_digest:
            report.todo_expected += 1
            if msg_id in task_keys:
                report.todo_created += 1
        if expected.get("deadline") and msg_id in in_digest:
            report.deadline_expected += 1
            if msg_id in deadline_parsed_ids:
                report.deadline_parsed += 1

    report.per_kind = per_kind
    summary = store.llm_call_summary(hours=24 * 365)
    report.llm_calls = int(summary.get("calls") or 0)
    report.llm_failed = int(summary.get("failed") or 0)
    report.llm_retried = int(summary.get("retried") or 0)
    report.prompt_tokens = int(summary.get("prompt_tokens") or 0)
    report.completion_tokens = int(summary.get("completion_tokens") or 0)
    report.cost_yuan = llmstats.call_cost(
        report.prompt_tokens, report.completion_tokens, price_in=report.price_in, price_out=report.price_out
    )
    return report


def run(
    *,
    count: int = DEFAULT_COUNT,
    seed: int = DEFAULT_SEED,
    hours: float = 16.0,
    records: list[dict[str, Any]] | None = None,
    data_dir: Path | str | None = None,
    **options: Any,
) -> BenchmarkReport:
    """生成（或用给定的）评测集跑一遍评测；不指定 `data_dir` 就用临时目录。"""
    dataset = records if records is not None else generate(count, seed=seed, span_hours=hours)
    if data_dir is None:
        with tempfile.TemporaryDirectory(prefix="qq-digest-bench-") as tmp:
            return evaluate(dataset, data_dir=tmp, seed=seed, **options)
    return evaluate(dataset, data_dir=data_dir, seed=seed, **options)


__all__ = [
    "DEFAULT_COUNT",
    "DEFAULT_SEED",
    "DETECTED_OUTCOMES",
    "DELIVERED_OUTCOMES",
    "BenchmarkReport",
    "evaluate",
    "generate",
    "read_fixture",
    "run",
    "write_fixture",
]
