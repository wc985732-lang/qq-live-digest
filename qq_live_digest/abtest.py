"""Prompt / 模型 A/B 对比（Roadmap A22）。

在**同一份固定评测集**上跑多套配置，比较准确率（召回 / 误报 / 待办 / 截止 / 去重）、
成本与延迟，选出质量分更高、其次更省、再其次更快的那套。

- 配置既可以是预设名（见 `PRESETS`），也可以是 `{"label": ..., ...}` 字典；
- 可调的规则阈值（`min_score` / `window_minutes` / `max_items` / `quiet_hours` …）离线也会改变数字；
- `prompt_profile`（`terse` / `detailed`）与 `model` 只有在配了 LLM Provider 时才真正影响输出——
  离线跑时它们只是被原样记录进结果，不会假装有效果。

纯函数 + 调用现有 `benchmark`，不联网、不写库。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from . import benchmark

# 预设名 → { label, options }；options 里的键会透给 benchmark / Settings。
PRESETS: dict[str, dict[str, Any]] = {
    "default": {"label": "默认", "options": {}},
    "strict": {"label": "严格（min_score=4）", "options": {"min_score": 4}},
    "loose": {"label": "宽松（min_score=2）", "options": {"min_score": 2}},
    "wide": {"label": "宽合并窗（60 分钟）", "options": {"window_minutes": 60}},
    "narrow": {"label": "窄合并窗（15 分钟）", "options": {"window_minutes": 15}},
    "terse": {"label": "精简 Prompt", "options": {"prompt_profile": "terse"}},
    "detailed": {"label": "详尽 Prompt", "options": {"prompt_profile": "detailed"}},
}

# 质量分 =（召回 + 待办 + 截止 + 去重 − 误报率）/ 5，等价于给误报一个负权重，
# 这样「更严但更准」的配置才可能胜过「更全但更吵」的配置。
QUALITY_KEYS = ("recall", "todo_rate", "deadline_rate", "dedupe_rate")

BENCH_OPTIONS = {
    "quiet_hours",
    "window_minutes",
    "daily_budget",
    "max_items",
    "min_score",
    "step_minutes",
    "price_in",
    "price_out",
}


def resolve(spec: Any) -> dict[str, Any]:
    """把预设名或字典统一成 {name, label, options}。"""
    if isinstance(spec, dict):
        options = {key: value for key, value in spec.items() if key not in {"label", "name"}}
        label = str(spec.get("label") or spec.get("name") or "自定义")
        name = str(spec.get("name") or label)
        return {"name": name, "label": label, "options": options}
    key = str(spec)
    if key not in PRESETS:
        raise KeyError(f"未知配置：{key}（可选：{', '.join(sorted(PRESETS))}）")
    preset = PRESETS[key]
    return {"name": key, "label": str(preset["label"]), "options": dict(preset["options"])}


def parse_spec(value: Any) -> Any:
    """CLI 用：以 `{` 开头就当 JSON 对象，否则当预设名。"""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text.startswith("{"):
        return text
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"配置不是合法 JSON：{error}") from error
    if not isinstance(data, dict):
        raise ValueError("配置 JSON 必须是对象")
    return data


def split_options(options: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """拆成 benchmark 能吃的参数与 LLM 侧的旋钮（prompt / model）。"""
    bench = {key: value for key, value in options.items() if key in BENCH_OPTIONS}
    extra = {key: value for key, value in options.items() if key not in BENCH_OPTIONS}
    return bench, extra


def metrics(report: benchmark.BenchmarkReport) -> dict[str, Any]:
    return {
        "messages": report.messages,
        "recall": round(report.recall, 4),
        "delivery_rate": round(report.delivery_rate, 4),
        "false_positive_rate": round(report.false_positive_rate, 4),
        "todo_rate": round(report.todo_rate, 4),
        "deadline_rate": round(report.deadline_rate, 4),
        "dedupe_rate": round(report.dedupe_rate, 4),
        "latency_median_min": round(report.latency_median_min, 3),
        "latency_p95_min": round(report.latency_p95_min, 3),
        "llm_calls": report.llm_calls,
        "cost_yuan": round(report.cost_yuan, 6),
    }


def quality(values: dict[str, Any]) -> float:
    positives = sum(float(values.get(key) or 0.0) for key in QUALITY_KEYS)
    penalty = float(values.get("false_positive_rate") or 0.0)
    return (positives - penalty) / (len(QUALITY_KEYS) + 1)


def _rank(entry: dict[str, Any]) -> tuple[float, float, float]:
    return (
        -round(float(entry["quality"]), 6),
        float(entry["metrics"]["cost_yuan"]),
        float(entry["metrics"]["latency_median_min"]),
    )


def compare(
    specs: Iterable[Any],
    *,
    count: int = benchmark.DEFAULT_COUNT,
    seed: int = benchmark.DEFAULT_SEED,
    hours: float = 16.0,
    records: list[dict[str, Any]] | None = None,
    base_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """在同一份评测集上跑每套配置，返回对比报告。"""
    dataset = records if records is not None else benchmark.generate(count, seed=seed, span_hours=hours)
    base = dict(base_options or {})
    entries: list[dict[str, Any]] = []
    for spec in specs:
        cfg = resolve(spec)
        options = {**base, **cfg["options"]}
        bench_options, llm_knobs = split_options(options)
        report = benchmark.run(
            records=dataset, count=count, seed=seed, hours=hours, **bench_options
        )
        values = metrics(report)
        entries.append(
            {
                "name": cfg["name"],
                "label": cfg["label"],
                "config": options,
                "llm_knobs": llm_knobs,
                "metrics": values,
                "quality": round(quality(values), 4),
            }
        )
    if not entries:
        raise ValueError("至少要有一套配置")
    winner = min(entries, key=_rank)
    delta: dict[str, float] = {}
    if len(entries) >= 2:
        first, second = entries[0]["metrics"], entries[1]["metrics"]
        delta = {
            key: round(float(first.get(key) or 0.0) - float(second.get(key) or 0.0), 4)
            for key in ("recall", "false_positive_rate", "todo_rate", "deadline_rate",
                        "dedupe_rate", "latency_median_min", "cost_yuan")
        }
    return {
        "ok": True,
        "dataset": {"messages": len(dataset), "seed": seed},
        "llm_configured": any(entry["llm_knobs"] for entry in entries),
        "configs": entries,
        "winner": winner["name"],
        "delta": delta,
    }


def render(report: dict[str, Any]) -> str:
    """终端表格：每套配置一行，最后给结论。"""
    header = f"{'配置':<18}{'质量分':>8}{'召回':>8}{'误报':>8}{'待办':>8}{'去重':>8}{'中位延迟':>10}{'成本':>8}"
    lines = [
        f"A/B：{report['dataset']['messages']} 条消息 / seed={report['dataset']['seed']}"
        + ("（含 LLM 侧旋钮）" if report.get("llm_configured") else "（未配 LLM，prompt/model 旋钮不影响数字）"),
        header,
    ]
    for entry in report["configs"]:
        m = entry["metrics"]
        star = " ★" if entry["name"] == report["winner"] else ""
        lines.append(
            f"{entry['label'][:16]:<18}{entry['quality']:>8.3f}{m['recall']:>8.3f}"
            f"{m['false_positive_rate']:>8.3f}{m['todo_rate']:>8.3f}{m['dedupe_rate']:>8.3f}"
            f"{m['latency_median_min']:>10.1f}{m['cost_yuan']:>8.4f}{star}"
        )
    winner = next(entry for entry in report["configs"] if entry["name"] == report["winner"])
    lines.append(f"结论：{winner['label']} 胜出（质量分 {winner['quality']:.3f}）。")
    if report.get("delta"):
        delta = report["delta"]
        lines.append(
            f"A−B：召回 {delta['recall']:+.3f} · 误报 {delta['false_positive_rate']:+.3f} · "
            f"待办 {delta['todo_rate']:+.3f} · 去重 {delta['dedupe_rate']:+.3f} · "
            f"中位延迟 {delta['latency_median_min']:+.1f} 分钟 · 成本 {delta['cost_yuan']:+.4f}"
        )
    return "\n".join(lines)


__all__ = [
    "PRESETS",
    "QUALITY_KEYS",
    "compare",
    "metrics",
    "parse_spec",
    "quality",
    "render",
    "resolve",
    "split_options",
]
