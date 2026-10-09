"""消息处理可观测面板（Roadmap A32）。

把「这一窗口的消息都去哪了」压成几组一眼能看的数字：

- **收**：入库消息数与涉及的群数（群号一律掩码）；
- **判**：决策结论分布 → 过滤率、候选量、去重 / 超限 / 入口拒绝；
- **调**：模型调用次数 / 失败 / 跳过 / 输入输出 token；
- **推**：投递成功率（成功 / 失败 / 待发）；
- **办**：候选确认率、完成率、逾期。

数据全部来自 `store` 已有的查询，这里是**纯聚合 + 纯渲染**：不联网、不写库。
默认脱敏：只输出计数与掩码后的群号，绝不带消息正文、发送者、token 或密钥。

三处出口共用同一份数字：`main.py observe`（终端）、`/panel`（网页）、
`/api/panel`（给脚本消费），所以看到的永远不会互相打架。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

from . import decisions as decisions_mod
from . import llmstats
from . import redact
from .timeutil import iso, now_local

#: 预设视图（天）：1 天 = 按日、7 天 = 按周、30 天 = 按月
VIEWS = (1, 7, 30)
DEFAULT_DAYS = 7
MAX_DAYS = 365
VIEW_LABELS = {1: "按日", 7: "按周", 30: "按月"}

PAGE_TITLE = "消息处理面板"


def clamp_days(value: Any) -> int:
    """把 `--days` 收敛到 1..365；认不出的值退回默认 7 天。"""
    try:
        days = int(value)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return max(1, min(MAX_DAYS, days))


def view_label(days: int) -> str:
    return VIEW_LABELS.get(int(days), f"最近 {int(days)} 天")


def mask_id(value: Any) -> str:
    """群号脱敏：只保留首尾各 2 位（与 doctor 同一规则）。"""
    return redact.mask_id(value, empty="（无群号）")


def rate(part: int, whole: int) -> float:
    """百分比（保留一位小数）；分母为 0 时返回 0.0，不假装 100%。"""
    if int(whole) <= 0:
        return 0.0
    return round(int(part) * 100 / int(whole), 1)


def filter_rate(counts: Mapping[str, int]) -> float:
    """过滤率 = 未命中 / 已决结论；过程态（待投递 / 延后未决）不进分母。"""
    total = sum(
        int(counts.get(key) or 0) for key in decisions_mod.OUTCOMES if decisions_mod.is_final(key)
    )
    return rate(int(counts.get(decisions_mod.FILTERED) or 0), total)


def push_rate(push: Mapping[str, int]) -> float:
    """推送成功率 = 成功 / (成功 + 失败)；还在队列里的待发不算分母。"""
    sent = int(push.get("sent") or 0)
    failed = int(push.get("failed") or 0)
    return rate(sent, sent + failed)


def snapshot(store: Any, *, days: int = DEFAULT_DAYS, now: dt.datetime | None = None) -> dict[str, Any]:
    """汇总一个窗口的面板数据。只读 store，不联网、不写库。"""
    days = clamp_days(days)
    moment = now or now_local()
    start = iso(moment - dt.timedelta(days=days))
    end = iso(moment)
    # 查询用半开区间 [start, end)，但同一条消息可能就落在「这一秒」；
    # 收口时补 1 秒，免得刚写进来的事件被挡在窗口外（对外展示仍是 moment）。
    query_end = iso(moment + dt.timedelta(seconds=1))
    counts = store.decision_counts(hours=days * 24)
    final, undecided = decisions_mod.split_counts(counts)
    candidates = sum(
        int(final.get(key) or 0) for key in (decisions_mod.PUSHED, decisions_mod.HELD)
    )
    candidates += int(undecided.get(decisions_mod.PENDING) or 0)
    push = store.delivery_metrics(start=start, end=query_end)
    return {
        "view": view_label(days),
        "days": days,
        "start": start,
        "end": end,
        "messages": store.message_metrics(start=start, end=query_end),
        "final": final,
        "undecided": undecided,
        "decided": sum(int(value or 0) for value in final.values()),
        "candidates": candidates,
        "filter_rate": filter_rate(counts),
        "llm": store.llm_call_summary(hours=days * 24),
        "push": push,
        "push_rate": push_rate(push),
        "tasks": store.task_metrics(start=start, end=query_end),
    }


def payload(snap: Mapping[str, Any]) -> dict[str, Any]:
    """JSON 友好（也是脱敏后）的面板数据：只有计数与掩码群号。"""
    messages = snap.get("messages") or {}
    llm = snap.get("llm") or {}
    push = snap.get("push") or {}
    prompt = int(llm.get("prompt_tokens") or 0)
    completion = int(llm.get("completion_tokens") or 0)
    return {
        "view": str(snap.get("view") or ""),
        "days": int(snap.get("days") or 0),
        "start": str(snap.get("start") or ""),
        "end": str(snap.get("end") or ""),
        "messages": {
            "total": int(messages.get("total") or 0),
            "groups": int(messages.get("distinct_groups") or 0),
            "top": [
                {"group": mask_id(row.get("group_id")), "messages": int(row.get("messages") or 0)}
                for row in (messages.get("top_groups") or [])
            ],
        },
        "decisions": {
            "counts": {str(key): int(value or 0) for key, value in (snap.get("final") or {}).items()},
            "undecided": {
                str(key): int(value or 0) for key, value in (snap.get("undecided") or {}).items()
            },
            "decided": int(snap.get("decided") or 0),
            "candidates": int(snap.get("candidates") or 0),
            "filter_rate": float(snap.get("filter_rate") or 0.0),
        },
        "llm": {
            "calls": int(llm.get("calls") or 0),
            "failed": int(llm.get("failed") or 0),
            "skipped": int(llm.get("skipped") or 0),
            "retried": int(llm.get("retried") or 0),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "tokens": prompt + completion,
        },
        "push": {
            "sent": int(push.get("sent") or 0),
            "failed": int(push.get("failed") or 0),
            "pending": int(push.get("pending") or 0),
            "rate": float(snap.get("push_rate") or 0.0),
        },
        "tasks": {str(key): value for key, value in (snap.get("tasks") or {}).items()},
    }


def _short(value: Any) -> str:
    text = str(value or "")
    return text[5:16].replace("T", " ") if len(text) >= 16 else text


def _breakdown(counts: Mapping[str, int], *, limit: int = 4) -> str:
    items = sorted(
        ((str(key), int(value or 0)) for key, value in counts.items() if int(value or 0)),
        key=lambda pair: -pair[1],
    )
    return " · ".join(f"{decisions_mod.outcome_label(key)} {value}" for key, value in items[:limit])


def render_text(snap: Mapping[str, Any]) -> str:
    """给人读的面板正文（CLI 与 doctor 共用同一份数字）。"""
    data = payload(snap)
    messages = data["messages"]
    dec = data["decisions"]
    llm = data["llm"]
    push = data["push"]
    tasks = data["tasks"]
    lines = [
        f"消息处理面板 · {data['view']}（{_short(data['start'])} → {_short(data['end'])}）",
        f"  收：{messages['total']:,} 条 · 涉及 {messages['groups']} 个群",
        f"  判：已决 {dec['decided']:,} 条 · 过滤率 {dec['filter_rate']}%"
        + (f" · {_breakdown(dec['counts'])}" if dec["counts"] else ""),
        f"  候选：{dec['candidates']:,} 条",
        f"  调：模型调用 {llm['calls']:,} 次 · 失败 {llm['failed']} · 跳过 {llm['skipped']}"
        f" · token {llmstats.format_tokens(llm['prompt_tokens'])}"
        f"+{llmstats.format_tokens(llm['completion_tokens'])}",
        f"  推：成功 {push['sent']} / 失败 {push['failed']} · 成功率 {push['rate']}%"
        + (f" · 待发 {push['pending']}" if push["pending"] else ""),
        f"  办：新候选 {int(tasks.get('candidates') or 0)}"
        f" · 确认 {int(tasks.get('confirmed') or 0)}（{float(tasks.get('confirmation_rate') or 0)}%）"
        f" · 完成 {int(tasks.get('completions') or 0)}（完成率 {float(tasks.get('completion_rate') or 0)}%）"
        f" · 逾期 {int(tasks.get('overdue') or 0)}",
    ]
    if messages["top"]:
        top = " · ".join(f"{row['group']} {row['messages']} 条" for row in messages["top"][:5])
        lines.append(f"  群消息 TOP：{top}")
    return "\n".join(lines)


def headline(snap: Mapping[str, Any]) -> str:
    """一句话版本，给 doctor 的一行摘要用。"""
    data = payload(snap)
    return (
        f"{data['days']} 天收到 {data['messages']['total']:,} 条"
        f" · 过滤率 {data['decisions']['filter_rate']}%"
        f" · 候选 {data['decisions']['candidates']}"
        f" · 推送成功率 {data['push']['rate']}%"
    )


def render_page(title: str = PAGE_TITLE) -> str:
    """极简面板页：只发一个请求，数字全部来自 `/api/panel`（已脱敏）。"""
    return PAGE_HTML.replace("__TITLE__", str(title or PAGE_TITLE))


PAGE_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0b0d12">
<title>__TITLE__</title>
<style>
:root{--bg:#0a0c12;--surface:rgba(255,255,255,.055);--line:rgba(255,255,255,.09);--text:#f3f5fa;--muted:#9aa4b5}
*{box-sizing:border-box}
body{margin:0;background:linear-gradient(180deg,#111728,var(--bg) 320px);color:var(--text);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:760px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:19px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin-bottom:16px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin-bottom:12px}
.card h2{font-size:13px;margin:0 0 8px;color:var(--muted);font-weight:600}
.card dl{margin:0;display:grid;grid-template-columns:auto 1fr;gap:6px 14px}
.card dt{color:var(--muted);font-size:13px}
.card dd{margin:0;font-variant-numeric:tabular-nums;word-break:break-all}
a{color:#8bb4ff}
</style></head>
<body><main>
<h1>__TITLE__</h1>
<div class="sub" id="window">加载中…</div>
<div id="cards"></div>
<div class="sub"><a href="/" id="back">← 回到待办台</a> · 群号已脱敏，这里只有计数</div>
</main>
<script>
const qs = new URLSearchParams(location.search);
const token = qs.get('token') || '';
const params = new URLSearchParams();
if (token) params.set('token', token);
document.getElementById('back').href = token ? '/?' + params.toString() : '/';
const pct = v => (v == null ? '0' : String(v)) + '%';
function card(title) {
  const box = document.createElement('section');
  box.className = 'card';
  const h = document.createElement('h2');
  h.textContent = title;
  const dl = document.createElement('dl');
  box.append(h, dl);
  document.getElementById('cards').append(box);
  return dl;
}
function row(dl, label, value) {
  const dt = document.createElement('dt');
  dt.textContent = label;
  const dd = document.createElement('dd');
  dd.textContent = value;
  dl.append(dt, dd);
}
fetch('/api/panel' + (params.toString() ? '?' + params.toString() : ''))
  .then(r => r.json())
  .then(d => {
    document.getElementById('window').textContent =
      d.view + '（' + d.start.replace('T', ' ') + ' → ' + d.end.replace('T', ' ') + '）';
    const m = card('收到');
    row(m, '消息', d.messages.total + ' 条');
    row(m, '涉及群', d.messages.groups + ' 个');
    if (d.messages.top.length) row(m, '群消息 TOP', d.messages.top.map(t => t.group + ' ' + t.messages).join(' · '));
    const j = card('判定');
    row(j, '已决', d.decisions.decided + ' 条');
    row(j, '过滤率', pct(d.decisions.filter_rate));
    row(j, '候选量', d.decisions.candidates + ' 条');
    row(j, '结论分布', Object.entries(d.decisions.counts).map(e => e[0] + ' ' + e[1]).join(' · ') || '无');
    const l = card('模型调用');
    row(l, '调用', d.llm.calls + ' 次');
    row(l, '失败 / 跳过', d.llm.failed + ' / ' + d.llm.skipped);
    row(l, 'token', d.llm.prompt_tokens + '+' + d.llm.completion_tokens);
    const p = card('推送');
    row(p, '成功 / 失败', d.push.sent + ' / ' + d.push.failed);
    row(p, '成功率', pct(d.push.rate));
    if (d.push.pending) row(p, '待发', d.push.pending + ' 条');
    const t = card('待办');
    row(t, '新候选', (d.tasks.candidates || 0) + ' 条');
    row(t, '确认率', pct(d.tasks.confirmation_rate));
    row(t, '完成率', pct(d.tasks.completion_rate));
    row(t, '逾期', (d.tasks.overdue || 0) + ' 条');
  })
  .catch(e => { document.getElementById('window').textContent = '加载失败：' + e; });
</script>
</body></html>
"""


__all__ = [
    "DEFAULT_DAYS",
    "MAX_DAYS",
    "PAGE_TITLE",
    "VIEWS",
    "clamp_days",
    "filter_rate",
    "headline",
    "mask_id",
    "payload",
    "push_rate",
    "rate",
    "render_page",
    "render_text",
    "snapshot",
    "view_label",
]
