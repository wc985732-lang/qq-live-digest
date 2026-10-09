#!/usr/bin/env python3
"""qq-live-digest 命令行入口。

常用：
    python main.py run            # 常驻监听 + 汇总 + 推送
    python main.py tick           # 只跑一次调度（适合任务计划程序定时调用）
    python main.py preview        # 预览待处理消息会推什么，不发送
    python main.py send-test      # 发送一条测试推送，验证通道
    python main.py doctor         # 自检配置、数据库、机器人凭证
    python main.py stats          # 查看消息/摘要/投递统计
    python main.py llm-stats      # 模型用量与成本：日 / 周 / 月视图
    python main.py benchmark      # 跑脱敏评测集，输出召回 / 误报 / 延迟 / 成本基线
    python main.py feedback       # 人工反馈回收：候选确认 / 忽略 / 纠错闭环
    python main.py groups         # 群级策略：每个群最终生效的开关（安静群 / 关键词 / 最低分 / 模型档）
    python main.py observe      # 可观测面板：过滤率 / 候选量 / 模型调用 / 推送成功率（脱敏）
    python main.py attach-test X  # 解析一个群文件/图片，只打印摘要不推送
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest.config import Settings, ensure_dirs  # noqa: E402
from qq_live_digest.doctor import (  # noqa: E402
    FAIL,
    DoctorContext,
    as_dicts,
    render,
    run_checks,
    worst_status,
)
from qq_live_digest.attachments import IMAGE_EXTS, Attachment  # noqa: E402
from qq_live_digest import confidence  # noqa: E402
from qq_live_digest import conflicts  # noqa: E402
from qq_live_digest import decisions  # noqa: E402
from qq_live_digest import events  # noqa: E402
from qq_live_digest import grouppolicy  # noqa: E402
from qq_live_digest import ics  # noqa: E402
from qq_live_digest import llmstats  # noqa: E402
from qq_live_digest import mcp  # noqa: E402
from qq_live_digest import observe  # noqa: E402
from qq_live_digest import restapi  # noqa: E402
from qq_live_digest.logging_setup import setup_logging  # noqa: E402
from qq_live_digest.service import DigestService  # noqa: E402
from qq_live_digest.store import CORRECTION_LABELS, Store  # noqa: E402
from qq_live_digest.weburl import build_web_url  # noqa: E402
from qq_digest import Message  # noqa: E402
from qq_live_digest.summarizer import Digest, finalize_digest, preview_text  # noqa: E402
from qq_live_digest.timeutil import iso, now_local  # noqa: E402

DEFAULT_ENV = PROJECT_ROOT / ".env"


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


def load_settings(args: argparse.Namespace) -> Settings:
    env_file = Path(args.env).expanduser() if getattr(args, "env", "") else DEFAULT_ENV
    return Settings.from_env(env_file=env_file)


def build_service(settings: Settings, *, console: bool = True) -> DigestService:
    ensure_dirs(settings)
    setup_logging(settings.log_dir, level=logging.INFO, console=console)
    return DigestService(settings)


def acquire_lock(path: Path):
    """单实例锁：避免任务计划程序与手动启动同时跑两份服务。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def command_run(args: argparse.Namespace) -> int:
    settings = load_settings(args)
    ensure_dirs(settings)
    lock = acquire_lock(settings.data_dir / "service.lock")
    if lock is None:
        print("已有实例在运行（service.lock 被占用），本次退出。")
        return 3
    try:
        service = build_service(settings)
        for problem in settings.problems():
            logging.getLogger("main").warning("配置提示：%s", problem)
        logging.getLogger("main").info("配置：%s", json.dumps(settings.describe(), ensure_ascii=False))
        service.run_forever(poll_seconds=args.poll)
    finally:
        lock.close()
    return 0


def command_tick(args: argparse.Namespace) -> int:
    settings = load_settings(args)
    service = build_service(settings)
    start_bot = not args.no_bot
    if start_bot:
        service.start()
    produced = service.tick()
    if start_bot:
        service.stop()
    for digest in produced:
        logging.getLogger("main").info(
            "本轮产出 digest#%s kind=%s items=%d messages=%d",
            digest.id,
            digest.kind,
            len(digest.items),
            digest.message_count,
        )
    return 0


def command_preview(args: argparse.Namespace) -> int:
    settings = load_settings(args)
    service = build_service(settings, console=False)
    limit = args.limit or settings.max_batch
    records = service.store.unprocessed_messages(limit=limit)
    print(preview_text(settings, records))
    return 0


def command_stats(args: argparse.Namespace) -> int:
    settings = load_settings(args)
    service = build_service(settings, console=False)
    print(json.dumps(service.status(), ensure_ascii=False, indent=2))
    return 0


def command_show(args: argparse.Namespace) -> int:
    """回看最近几条摘要，以及每条判断所依据的原文句子。"""
    settings = load_settings(args)
    service = build_service(settings, console=False)
    digests = service.store.recent_digests(limit=args.limit or 5)
    if not digests:
        print("还没有已归档的摘要。")
        return 0
    for digest in digests:
        items = digest.get("items") or []
        print(
            f"#{digest['id']} [{digest['kind']}] {digest['window_start']} ~ {digest['window_end']}"
            f" · {digest['item_count']} 条重点 / {digest['message_count']} 条消息"
        )
        for index, item in enumerate(items, start=1):
            summary = str(item.get("summary") or item.get("text") or "")[:60]
            head = f"  {index}. [{item.get('category') or 'info'}] {summary}"
            if item.get("deadline"):
                head += f"（截止 {item['deadline']}）"
            print(head)
            action = str(item.get("action") or "")
            if action and action != summary:
                print(f"     需做：{action}")
            if item.get("group"):
                print(f"     来源：{item['group']}")
            if item.get("evidence"):
                print(f"     依据：{item['evidence']}")
            info = item.get("confidence") or {}
            why = str(item.get("why") or info.get("why") or "")
            if info or why:
                note = str(info.get("text") or "")
                suffix = f"（{note}）" if note else ""
                print(f"     为什么：{why or '没有命中任何规则'}{suffix}")
            else:
                print("     为什么：这条没有置信度记录（早于 A7 归档，或不属于通知候选）。")
        print()
    return 0


def command_decisions(args: argparse.Namespace) -> int:
    """回看决策日志：每条消息为什么被推 / 没被推。"""
    settings = load_settings(args)
    service = build_service(settings, console=False)
    store = service.store

    if args.msg_id:
        rows = store.decisions_for(args.msg_id, limit=args.limit or 50)
        if not rows:
            print(f"没有 {args.msg_id} 的决策记录：可能早于本功能上线，或这条消息还没进入处理窗口。")
            return 0
        print(f"消息 {args.msg_id} 的决策轨迹（新 → 旧）：")
        for line in decisions.describe_rows(rows):
            print(line)
        return 0

    hours = args.hours or 24
    final_counts, undecided = decisions.split_counts(store.decision_counts(hours=hours))
    if final_counts:
        print(f"最近 {hours} 小时的已决分布：" + decisions.summarise_counts(final_counts))
    deferred = int(undecided.get(decisions.DEFERRED) or 0)
    if deferred:
        print(
            f"另有 {deferred} 条「延后未决」：本该推送，但被夜间静默 / 当日额度 / 大模型失败推迟，"
            "下个窗口会再试。"
        )
        print("  看明细：main.py decisions --outcome deferred")
    if final_counts or deferred:
        print()
    rows = store.recent_decisions(limit=args.limit or 20, outcome=args.outcome or "")
    if not rows:
        print("还没有决策记录。")
        return 0
    for line in decisions.describe_rows(rows):
        print(line)
    return 0



def command_llm_stats(args: argparse.Namespace) -> int:
    """看模型用量与成本：日 / 周 / 月视图（Roadmap A5）。

    数据来自 `llm_calls` 表（每次调用记 provider/model/token/耗时/失败原因/是否重试降级）；
    费用按当前配置的单价（元 / 百万 token）在展示时折算，改价不用重写历史。
    """
    settings = load_settings(args)
    service = build_service(settings, console=False)
    store = service.store
    try:
        period = llmstats.parse_period(args.period)
    except ValueError as error:
        print(str(error))
        return 2
    count = max(1, int(args.buckets or llmstats.DEFAULT_BUCKETS.get(period, 14)))
    now = now_local()
    start = llmstats.window_start(period, count, now=now)
    rows = store.llm_calls_since(start)
    buckets = llmstats.group_rows(rows, period, now=now, count=count)
    totals = llmstats.totals_from_rows(rows)
    price_in = float(settings.llm_price_in or 0.0)
    price_out = float(settings.llm_price_out or 0.0)

    if args.json:
        print(
            json.dumps(
                {
                    "period": period,
                    "window_start": iso(start),
                    "buckets": buckets,
                    "totals": totals,
                    "price": {"in_per_million": price_in, "out_per_million": price_out},
                    "top_errors": llmstats.top_errors(rows, limit=5),
                    "routes": llmstats.route_counts(rows),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(
        f"模型用量 · 按{llmstats.PERIOD_LABELS[period]}"
        f"（最近 {count} 个周期，{iso(start).replace('T', ' ')[:16]} 起）"
    )
    if price_in or price_out:
        print(f"单价：输入 ¥{price_in:g} / 百万 token · 输出 ¥{price_out:g} / 百万 token")
    else:
        print(
            "单价未配置（QQ_DIGEST_LLM_PRICE_IN / QQ_DIGEST_LLM_PRICE_OUT，单位：元/百万 token），"
            "当前只统计 token。"
        )
    print()
    for line in llmstats.render_table(buckets, price_in=price_in, price_out=price_out):
        print(line)
    print()
    print("合计：" + llmstats.summarise_bucket(totals, price_in=price_in, price_out=price_out))
    if int(totals.get("calls") or 0):
        print(f"用途分布：{llmstats.breakdown(totals.get('purposes') or {}, labels=llmstats.PURPOSE_LABELS)}")
        print(f"模型分布：{llmstats.breakdown(totals.get('models') or {})}")
        print(
            "路由分布："
            f"{llmstats.breakdown(llmstats.route_counts(rows), labels=llmstats.ROUTE_LABELS)}"
        )
    errors = llmstats.top_errors(rows, limit=3)
    if errors:
        print("失败原因 TOP：")
        for reason, times in errors:
            print(f"  {times}× {reason}")
    elif not int(totals.get("calls") or 0):
        print(
            "还没有用量记录：这段时间模型没被调用过。跑一次 `main.py tick` 或 `main.py simulate` 再看，"
            "也可以 `main.py doctor` 确认模型是否可用。"
        )
    if args.recent:
        recent = store.recent_llm_calls(limit=args.recent)
        if recent:
            print()
            print(f"最近 {len(recent)} 次调用（新 → 旧）：")
            for line in llmstats.describe_calls(recent, price_in=price_in, price_out=price_out):
                print(line)
    return 0


def command_feedback(args: argparse.Namespace) -> int:
    """人工反馈回收：候选确认 / 忽略 / 纠错的闭环（Roadmap A8）。

    数据来自待办事件表：候选被确认 / 忽略 / 纠错都算反馈。
    这里只汇总与解释，不自动改配置——「怎么改规则」仍由人决定。
    """
    settings = load_settings(args)
    service = build_service(settings, console=False)
    store = service.store
    days = max(1, int(args.days or 30))
    summary = store.feedback_summary(days=days)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    start = str(summary.get("window_start") or "").replace("T", " ")[:16]
    end = str(summary.get("window_end") or "").replace("T", " ")[:16]
    print(f"反馈回收 · 最近 {days} 天（{start} ~ {end}）")
    candidates = int(summary.get("candidates") or 0)
    if not candidates:
        print(
            "还没有待确认候选：判断都直接进了正式待办，暂时没有反馈可回收。"
            "等出现低置信候选后，在待办台「确认 / 忽略 / 纠错」即可把反馈收回来。"
        )
        return 0
    confirmed = int(summary.get("confirmed") or 0)
    dismissed = int(summary.get("dismissed") or 0)
    corrected = int(summary.get("corrected") or 0)
    print(
        f"候选 {candidates} 条 · 确认 {confirmed}"
        f"（{float(summary.get('confirmation_rate') or 0.0):g}%） · 忽略 {dismissed}"
        f"（{float(summary.get('dismissal_rate') or 0.0):g}%） · 纠错 {corrected} 次"
    )
    by_type = summary.get("by_type") or {}
    if by_type:
        parts = [
            f"{CORRECTION_LABELS.get(str(kind), str(kind))} ×{int(times or 0)}"
            for kind, times in sorted(by_type.items(), key=lambda item: -int(item[1] or 0))
        ]
        print("纠错类型：" + "、".join(parts))
    by_group = summary.get("by_group") or {}
    if by_group:
        ranked = sorted(
            by_group.items(), key=lambda item: -int((item[1] or {}).get("total") or 0)
        )
        print(f"按群（近 {days} 天纠错）：")
        for name, entry in ranked[:5]:
            entry = entry or {}
            detail = "、".join(
                f"{CORRECTION_LABELS.get(str(kind), str(kind))} {int(count or 0)}"
                for kind, count in sorted(entry.items(), key=lambda item: -int(item[1] or 0))
                if str(kind) != "total"
            )
            print(f"  {name}：共 {int(entry.get('total') or 0)} 次（{detail}）")
    insights = summary.get("insights") or []
    if insights:
        print("规则建议（只提建议，不自动改配置）：")
        for line in insights[:5]:
            print(f"  {line}")
    elif corrected:
        print("暂时没有成规模的纠错模式，继续收集反馈即可。")
    return 0


def command_groups(args: argparse.Namespace) -> int:
    """群级个性化策略（Roadmap A9）：每个群最终生效的开关分别是什么。"""
    settings = load_settings(args)
    groups = [str(item) for item in settings.group_whitelist if str(item or "").strip()]
    policies = dict(settings.group_policies or {})
    rows = []
    for group_id in groups:
        name = settings.group_name(group_id)
        policy = settings.group_policy(group_id, name)
        rows.append(
            {
                "group_id": group_id,
                "name": name,
                "matched": policy.matched,
                "overrides": list(policy.overrides),
                "quiet": policy.quiet,
                "keywords": list(policy.keywords),
                "min_score": policy.min_score,
                "model": policy.model,
                "model_label": policy.model_label,
                "quiet_hours": policy.quiet_hours,
                "describe": policy.describe(),
            }
        )
    used = {row["matched"] for row in rows if row["matched"]}
    unused = sorted(key for key in policies if key not in used)
    if args.json:
        print(
            json.dumps(
                {
                    "groups": rows,
                    "unused_policies": unused,
                    "fields": list(grouppolicy.FIELDS),
                    "models": dict(grouppolicy.MODEL_LABELS),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not rows:
        print("还没有配置任何群（QQ_DIGEST_GROUPS），没有群需要策略。")
        return 0
    print(f"群级策略 · {len(rows)} 个群（没写的字段继承全局）")
    for row in rows:
        if row["overrides"]:
            mark = "本群覆盖：" + "、".join(row["overrides"])
        else:
            mark = "全部继承全局"
        print(f"  {row['group_id']}（{row['name']}）  {row['describe']}")
        print(f"    {mark}")
    if unused:
        print("以下策略键没有匹配到白名单群：" + "、".join(unused))
        print("  键要写群号或群名，而且群必须在 QQ_DIGEST_GROUPS 白名单里才会被处理。")
    if not any(row["overrides"] for row in rows):
        print("提示：QQ_DIGEST_GROUP_POLICIES 是 JSON，例如")
        print('  {"123456": {"quiet": true, "min_score": 5, "keywords": ["考试"]}}')
    return 0

def _recent_event_merges(store, *, limit: int = 5) -> list[dict[str, Any]]:
    """从最近摘要里挑出发生过事件合并的条目（Roadmap A11），供 events 命令展示。"""
    rows: list[dict[str, Any]] = []
    for digest in store.recent_digests(limit=max(1, int(limit)) * 3):
        for item in digest.get("items") or []:
            if not isinstance(item, dict) or not item.get("event_merged"):
                continue
            groups = [
                str(name) for name in (item.get("duplicate_groups") or []) if str(name).strip()
            ]
            rows.append(
                {
                    "digest_id": int(digest.get("id") or 0),
                    "summary": str(item.get("summary") or ""),
                    "merged": max(0, len(set(groups)) - 1),
                    "groups": "、".join(dict.fromkeys(groups)),
                    "key": str(item.get("event_key") or ""),
                }
            )
            if len(rows) >= max(1, int(limit)):
                return rows
    return rows


def command_events(args: argparse.Namespace) -> int:
    """事件级跨群聚合（Roadmap A11）：开关、拆分覆盖与最近合并情况。"""
    settings = load_settings(args)
    service = build_service(settings, console=False)
    store = service.store

    if args.split:
        key = args.split.strip()
        if not store.add_event_split(key, reason=args.reason or "人工拆分"):
            print("拆分键不能为空。")
            return 2
        print(f"已记录拆分覆盖：{key}")
        print("这个事件之后不再自动合并；用 main.py events --unsplit <键> 撤销。")
        return 0

    if args.unsplit:
        key = args.unsplit.strip()
        if store.remove_event_split(key):
            print(f"已撤销拆分覆盖：{key}")
        else:
            print(f"没有找到拆分覆盖：{key}")
        return 0

    splits = store.event_splits()
    merges = _recent_event_merges(store, limit=args.limit or 5)
    if args.json:
        print(
            json.dumps(
                {
                    "enabled": bool(settings.event_merge),
                    "env": "QQ_DIGEST_EVENT_MERGE",
                    "splits": splits,
                    "recent_merges": merges,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    state = "已开启" if settings.event_merge else "未开启（设 QQ_DIGEST_EVENT_MERGE=1 打开）"
    print(f"事件级跨群聚合：{state}")
    if merges:
        print(f"最近摘要里的合并（{len(merges)} 条）：")
        for row in merges:
            print(f"  摘要 #{row['digest_id']} · {row['summary']}")
            print(f"    合并 {row['merged']} 处 · {row['groups']}")
            print(f"    事件键：{row['key']}")
    else:
        print("最近摘要里没有事件合并记录。")
    if splits:
        print(f"拆分覆盖 {len(splits)} 条（这些事件不再自动合并）：")
        for row in splits:
            extra = f" · {row['reason']}" if row["reason"] else ""
            print(f"  {row['key']}{extra} · {row['created_at']}")
    else:
        print("还没有拆分覆盖；误合并时：main.py events --split <事件键> --reason 说明")
    return 0


def command_conflicts(args: argparse.Namespace) -> int:
    """时间冲突检测（Roadmap A12）：截止时间相近的待办只提醒，不擅自改任务。"""
    settings = load_settings(args)
    ensure_dirs(settings)
    store = Store(
        settings.data_dir / "digest.sqlite3",
        retention_days=settings.message_retention_days,
    )
    report = conflicts.payload(
        store.list_tasks(),
        now=now_local(),
        window_minutes=args.window,
        include_done=args.include_done,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(conflicts.render(report))
    return 0


def command_ab(args: argparse.Namespace) -> int:
    """Prompt/模型 A/B（Roadmap A22）：固定评测集上对比两套配置的准确率 / 成本 / 延迟。"""
    from qq_live_digest import abtest

    if args.list_presets:
        print("内置预设（--a/--b 可用预设名，也可直接给 JSON 对象）：")
        for name, preset in abtest.PRESETS.items():
            print(f"  {name:<10} {preset['label']}")
        return 0
    try:
        specs = [abtest.parse_spec(args.a), abtest.parse_spec(args.b)]
    except ValueError as error:
        print(f"配置解析失败：{error}")
        return 2
    report = abtest.compare(specs, count=args.count, seed=args.seed)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(abtest.render(report))
    return 0


def command_ics(args: argparse.Namespace) -> int:
    """日历导出（Roadmap A13）：把有明确时间的待办导成 ICS，供手机 / 桌面日历订阅。"""
    settings = load_settings(args)
    ensure_dirs(settings)
    store = Store(
        settings.data_dir / "digest.sqlite3",
        retention_days=settings.message_retention_days,
    )
    tasks = store.list_tasks()
    now = now_local()
    report = ics.payload(tasks, now=now, include_done=args.include_done)
    if args.print:
        # ICS 用 CRLF 行尾，必须走字节流，避免 Windows 文本模式再把 \n 翻译成 \r\n。
        sys.stdout.buffer.write(
            ics.build_calendar(tasks, now=now, include_done=args.include_done).encode("utf-8")
        )
        sys.stdout.buffer.flush()
        return 0
    target = Path(args.out) if args.out else (settings.data_dir / "calendar.ics")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        ics.build_calendar(tasks, now=now, include_done=args.include_done).encode("utf-8")
    )
    if args.json:
        print(json.dumps({**report, "path": str(target)}, ensure_ascii=False, indent=2))
    else:
        print(f"已导出 {report['events']} 个日程（全天 {report['all_day']} 个）到 {target}")
        if report["first"]:
            print(f"时间范围：{report['first']} → {report['last']}")
    return 0


def command_mcp(args: argparse.Namespace) -> int:
    """只读 MCP 接口（Roadmap A1）：stdio 给 Cursor / Claude 查询通知、待办、截止与历史消息。"""
    settings = load_settings(args)
    if args.list_tools:
        print(
            json.dumps(
                {
                    "server": {"name": mcp.SERVER_NAME, "version": mcp.SERVER_VERSION},
                    "read_only": True,
                    "tools": mcp.list_tools(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    ensure_dirs(settings)
    store = Store(
        settings.data_dir / "digest.sqlite3",
        retention_days=settings.message_retention_days,
    )
    return mcp.serve_stdio(store)


def command_api(args: argparse.Namespace) -> int:
    """只读 REST API（Roadmap A15）：接口目录、鉴权状态与访问地址。"""
    settings = load_settings(args)
    payload = restapi.index_payload()
    host = str(settings.web_host or "127.0.0.1")
    port = int(settings.web_port or 0)
    server = {
        "enabled": bool(settings.web_enabled),
        "base_url": f"http://{host}:{port}",
        "host": host,
        "port": port,
        "auth": bool(settings.web_token),
    }
    if args.json:
        print(json.dumps({**payload, "server": server}, ensure_ascii=False, indent=2))
        return 0
    print(f"只读 REST API v{payload['api_version']}（只读：不写库、不联网、不推送）")
    state = "已开启" if server["enabled"] else "未开启（设 QQ_DIGEST_WEB=1）"
    print(f"  监听：{server['base_url']} · 待办台{state}")
    if server["auth"]:
        print("  鉴权：已配置 token（请求带 X-Token 头或 ?token=）")
    else:
        print("  鉴权：未配置 token（QQ_DIGEST_WEB_TOKEN）；非回环监听时会自动生成并落库")
    print("  接口：")
    for item in payload["endpoints"]:
        mark = "需要 token" if item["auth"] else "免 token"
        print(f"    {item['method']} {item['path']}  [{mark}] {item['desc']}")
    return 0


def command_observe(args: argparse.Namespace) -> int:
    """消息处理可观测面板（Roadmap A32）：过滤率 / 候选量 / 模型调用 / 推送成功率。

    数据来自决策日志、用量表与投递队列，全程只读；群号一律掩码，不打印正文、
    发送者与密钥，所以这段输出可以直接贴进 Issue。网页版挂在待办台的 `/panel`。
    """
    settings = load_settings(args)
    service = build_service(settings, console=False)
    days = observe.clamp_days(getattr(args, "days", observe.DEFAULT_DAYS))
    snap = observe.snapshot(service.store, days=days)
    if args.json:
        print(json.dumps(observe.payload(snap), ensure_ascii=False, indent=2))
        return 0
    print(observe.render_text(snap))
    if settings.web_enabled:
        panel_url = build_web_url(
            settings,
            token=str(settings.web_token or ""),
            base_url=build_web_url(settings).rstrip("/") + "/panel",
        )
        print(f"  网页版：{panel_url}（token 与待办台相同）")
    return 0

def command_simulate(args: argparse.Namespace) -> int:
    """用假群聊把整条链路跑一遍（Roadmap A20）：消息 → 判定 → 摘要 → 内存假通道。

    全程不联网、不读 `.env`、不用真实通道、不碰 `data/`，可以放心在 CI 与演示里跑。
    """
    from qq_live_digest import simulator

    if args.fixture:
        records = simulator.read_fixture(args.fixture)
        print(f"载入 fixture：{args.fixture}（{len(records)} 条消息）")
    else:
        records = simulator.generate(args.count, seed=args.seed, span_hours=args.hours)
        print(f"生成假群聊：{len(records)} 条消息 / {args.hours:g} 小时 / seed={args.seed}")
    if args.out:
        written = simulator.write_fixture(args.out, records)
        print(f"已写出 fixture：{args.out}（{written} 行 JSONL）")
    if args.write_only:
        return 0

    workdir = Path(args.dir) if args.dir else Path(tempfile.mkdtemp(prefix="qq-digest-sim-"))
    workdir.mkdir(parents=True, exist_ok=True)
    logging.getLogger("qq_live_digest").setLevel(logging.WARNING)
    report = simulator.replay(
        records,
        data_dir=workdir,
        quiet_hours=args.quiet_hours,
        window_minutes=args.window,
        daily_budget=args.budget,
        max_items=args.max_items,
        min_score=args.min_score,
    )
    print()
    for line in report.summary_lines():
        print(line)
    if report.first_body:
        print()
        print("—— 实际推给用户的第一条内容（群里全是编造的假数据）——")
        print(report.first_body)
    print()
    print(f"一次性数据库：{workdir}（跑完可直接删）")
    print(
        f"回查某条消息：$env:QQ_DIGEST_DATA_DIR='{workdir}'; "
        f"python main.py decisions --msg-id sim-{args.seed}-00001"
    )
    return 0


def command_benchmark(args: argparse.Namespace) -> int:
    """跑脱敏评测集，输出召回 / 误报 / 待办 / 截止时间 / 去重 / 延迟 / 成本（Roadmap A21）。

    评测集由 A20 模拟器生成（或从带 `_expect` 标注的 fixture 读），走真实链路回放，
    全程离线：内存假通道、不调大模型、默认用临时目录，不碰 `data/`。
    同 seed 必得同一组数字，可以作为回归基线。
    """
    from qq_live_digest import benchmark, simulator

    if args.fixture:
        records = simulator.read_fixture(args.fixture)
        print(f"载入评测集：{args.fixture}（{len(records)} 条消息）")
    else:
        records = simulator.generate(args.count, seed=args.seed, span_hours=args.hours)
        print(f"生成评测集：{len(records)} 条消息 / {args.hours:g} 小时 / seed={args.seed}")
    workdir = Path(args.dir) if args.dir else Path(tempfile.mkdtemp(prefix="qq-digest-bench-"))
    workdir.mkdir(parents=True, exist_ok=True)
    logging.getLogger("qq_live_digest").setLevel(logging.WARNING)
    report = benchmark.evaluate(
        records,
        data_dir=workdir,
        quiet_hours=args.quiet_hours,
        window_minutes=args.window,
        daily_budget=args.budget,
        max_items=args.max_items,
        min_score=args.min_score,
        seed=args.seed,
        price_in=args.price_in,
        price_out=args.price_out,
    )
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print()
        for line in report.summary_lines():
            print(line)
        print()
        print(f"一次性数据库：{workdir}（跑完可直接删）")
    if args.fail_under and report.recall < args.fail_under:
        print()
        print(f"召回 {report.recall * 100:.1f}% 低于门槛 {args.fail_under * 100:.1f}%")
        return 1
    return 0


def command_tasks(args: argparse.Namespace) -> int:
    """查看待办清单，并打印手机访问地址。"""
    settings = load_settings(args)
    service = build_service(settings, console=False)
    if args.reset:
        removed = service.store.clear_tasks()
        print(f"已清空 {removed} 条待办。")
    if args.import_existing:
        imported = service.import_existing_tasks(days=args.days)
        print(f"已回填 {imported} 条待办（来自最近 {args.days} 天摘要）。")
    limit = args.limit or 30
    statuses = ("candidate", "open", "done", "dismissed", "expired") if args.all else ("candidate", "open")
    tasks = service.store.list_tasks(statuses=statuses, limit=max(limit, 200))
    stats = service.store.task_stats()
    print(
        f"待办统计：待确认 {stats['candidate']} 条，未完成 {stats['open']} 条，"
        f"已完成 {stats['done']} 条，已忽略 {stats['dismissed']} 条。"
    )
    for task in tasks[:limit]:
        mark = {"done": "x", "candidate": "?", "dismissed": "-", "expired": "!"}.get(
            str(task.get("status") or ""), " "
        )
        deadline = str(task.get("deadline") or "")[:16].replace("T", " ")
        suffix = f"  截止 {deadline}" if deadline else ""
        print(f"  [{mark}] #{task.get('id')} {task.get('summary') or ''}{suffix}")
    token = settings.web_token or service.store.meta_get("web_token", "")
    url = build_web_url(settings, token=token)
    print()
    print(f"待办台：{url}")
    return 0


def command_catchup(args: argparse.Namespace) -> int:
    settings = load_settings(args)
    service = build_service(settings, console=True)
    inserted = service.run_catchup(force=True)
    print(f"历史补采完成，新增 {inserted} 条消息。")
    return 0


def _check_token(settings: Settings, timeout: int = 15) -> tuple[bool, str]:
    payload = json.dumps({"appId": settings.appid, "clientSecret": settings.secret}).encode("utf-8")
    request = urllib.request.Request(
        "https://bots.qq.com/app/getAppAccessToken",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.HTTPError as error:
        return False, f"HTTP {error.code}: {error.read().decode('utf-8', errors='replace')[:200]}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        return False, f"请求失败: {error}"
    if data.get("access_token"):
        return True, f"获取 access_token 成功（expires_in={data.get('expires_in')}）"
    return False, f"返回异常: {str(data)[:200]}"


def command_doctor(args: argparse.Namespace) -> int:
    configure_console()
    settings = load_settings(args)
    try:
        ensure_dirs(settings)
    except OSError as error:
        print(f"[FAIL] 数据目录不可用  {error}  → 检查磁盘空间与目录权限")
        return 1
    context = DoctorContext(
        settings=settings,
        bot_credential_check=(lambda: _check_token(settings, settings.http_timeout)) if args.online else None,
    )
    checks = run_checks(context)
    if args.json:
        print(json.dumps(as_dicts(checks), ensure_ascii=False, indent=2))
    else:
        print(render(checks))
        print("提示：加 --online 会额外在线校验 QQ 官方机器人凭证；加 --json 便于脚本处理。")
    return 1 if worst_status(checks) == FAIL else 0


def command_send_test(args: argparse.Namespace) -> int:
    settings = load_settings(args)
    service = build_service(settings)
    started = False
    if settings.official_bot_enabled and settings.appid and settings.secret:
        started = bool(service.bot.start())
        if started:
            ready = service.bot.wait_ready(timeout=args.wait)
            print(f"机器人就绪：{ready}")
    manager = service.push_manager()
    if not manager.pushers:
        print("没有可用推送通道，请先配置 .env")
        if started:
            service.stop()
        return 2
    title = "QQ群通知助手测试"
    stamp = now_local()
    deadline = stamp.replace(hour=18, minute=0, second=0, microsecond=0)

    def card_item(text: str, category: str, summary: str, action: str, due, group: str) -> dict:
        return {
            "message": Message(timestamp=stamp, sender="辅导员", text=text, source=group),
            "category": category,
            "importance": 5 if category == "urgent" else 4,
            "score": 5,
            "summary": summary,
            "action": action,
            "deadline_dt": due,
            "group_name": group,
            "evidence": text,
            "msg_id": "",
        }

    digest = Digest(
        kind="window",
        window_start=stamp,
        window_end=stamp,
        items=[
            card_item(
                "请各班班长今天18:00前提交实验报告",
                "action",
                "提交实验报告",
                "今晚18:00前交给班长",
                deadline,
                "测仪2602班群",
            ),
            card_item(
                "今晚务必完成安全知识答题",
                "urgent",
                "安全知识答题今晚截止",
                "立即完成答题",
                deadline,
                "电气学院2026级群",
            ),
            card_item(
                "【通知】图书馆周末闭馆",
                "info",
                "图书馆周末闭馆",
                "",
                None,
                "电气学院2026级群",
            ),
            card_item(
                "下周一开始选课",
                "academic",
                "下周一开始选课",
                "",
                None,
                "测仪2602班群",
            ),
        ],
        message_count=4,
        groups=["电气学院2026级群", "测仪2602班群"],
    )
    digest = finalize_digest(digest, settings)
    outcomes = manager.send_card(
        title,
        digest.body,
        dedupe_key=f"test:{iso(stamp)}",
        summary=digest.summary,
        html=digest.html,
    )
    failed = 0
    for item in outcomes:
        status = "OK" if item.ok else "失败"
        print(f"  {status} {item.channel}:{item.target} {item.error}")
        failed += 0 if item.ok else 1
    if started:
        service.stop()
    return 0 if failed == 0 else 1


def command_attach_test(args: argparse.Namespace) -> int:
    """解析群文件 / 本地图片并打印摘要，不写库也不推送。"""
    settings = load_settings(args)
    service = build_service(settings, console=True)
    worker = service.attachment_worker
    source = str(getattr(args, "path", "") or "").strip()
    record: dict | None = None
    if source:
        local = Path(source).expanduser()
        if not local.is_file():
            print(f"本地文件不存在：{local}")
            return 2
        attachment = Attachment(
            kind="image" if local.suffix.lower() in IMAGE_EXTS else "file",
            name=local.name,
            group_id=str(args.group or "local"),
            group_name=settings.group_name(str(args.group)) if args.group else "本地测试",
            sender_name="本地文件",
            size=local.stat().st_size,
        )
        record = worker.process_local(attachment, local)
    elif args.group and args.file_id:
        attachment = Attachment(
            kind="file",
            name=str(args.name or "群文件"),
            group_id=str(args.group),
            group_name=settings.group_name(str(args.group)),
            sender_name="群文件",
            file_id=str(args.file_id),
            busid=str(args.busid or ""),
        )
        record = worker.process(attachment)
    else:
        print(
            "用法：attach-test <本地文件路径>"
            " 或 attach-test --group <群号> --file-id <id> [--busid 102] [--name 文件名]"
        )
        return 2
    if not record:
        print("没有生成摘要（内容为空或被限流，详见日志）。")
        return 1
    print("==== 附件摘要 ====")
    print(f"来源：{record['group_name']} · {record['sender_name']}")
    print(record["content"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="QQ 群通知实时摘要推送")
    parser.add_argument("--env", default="", help=".env 文件路径，默认项目根目录 .env")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="常驻运行")
    run.add_argument("--poll", type=int, default=0, help="轮询秒数，默认读配置")
    run.set_defaults(func=command_run)

    tick = sub.add_parser("tick", help="执行一次调度")
    tick.add_argument("--no-bot", action="store_true", help="不启动机器人，只处理库里已有消息")
    tick.set_defaults(func=command_tick)

    preview = sub.add_parser("preview", help="预览待处理消息的推送内容")
    preview.add_argument("--limit", type=int, default=0)
    preview.set_defaults(func=command_preview)

    stats = sub.add_parser("stats", help="查看统计")
    stats.set_defaults(func=command_stats)

    show = sub.add_parser("show", help="回看最近几条摘要及判断依据")
    show.add_argument("--limit", type=int, default=5)
    show.set_defaults(func=command_show)

    why = sub.add_parser("decisions", aliases=["why"], help="决策日志：为什么推 / 为什么不推")
    why.add_argument("--msg-id", dest="msg_id", default="", help="只看某条消息的完整决策轨迹")
    why.add_argument("--outcome", default="", help="按结论过滤（filtered / deduped / truncated / rejected / duplicate / pushed / held）")
    why.add_argument("--hours", type=int, default=24, help="统计最近多少小时的结论分布")
    why.add_argument("--limit", type=int, default=0, help="最多显示多少条（列表默认 20，按消息查默认 50）")
    why.set_defaults(func=command_decisions)

    usage = sub.add_parser("llm-stats", aliases=["cost"], help="模型用量与成本：日 / 周 / 月视图")
    usage.add_argument("--period", default="day", help="统计粒度：day（默认）/ week / month")
    usage.add_argument("--buckets", type=int, default=0, help="显示多少个周期（默认 日 14 / 周 8 / 月 6）")
    usage.add_argument("--recent", type=int, default=0, help="额外列出最近 N 条调用明细")
    usage.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本消费")
    usage.set_defaults(func=command_llm_stats)

    review = sub.add_parser("feedback", aliases=["review"], help="人工反馈回收：候选确认 / 忽略 / 纠错闭环")
    review.add_argument("--days", type=int, default=30, help="统计最近多少天（默认 30）")
    review.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本消费")
    review.set_defaults(func=command_feedback)

    groups = sub.add_parser("groups", aliases=["policies"], help="群级策略：每个群最终生效的开关")
    groups.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本消费")
    groups.set_defaults(func=command_groups)

    events_cmd = sub.add_parser("events", aliases=["aggregate"], help="事件级跨群聚合（A11）：开关、拆分覆盖、最近合并")
    events_cmd.add_argument("--split", default="", help="把某个事件键标记为「不要自动合并」")
    events_cmd.add_argument("--unsplit", default="", help="撤销某个事件键的拆分覆盖")
    events_cmd.add_argument("--reason", default="", help="拆分原因（可选）")
    events_cmd.add_argument("--limit", type=int, default=5, help="最多展示多少条近期合并（默认 5）")
    events_cmd.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本消费")
    events_cmd.set_defaults(func=command_events)

    api_cmd = sub.add_parser("api", aliases=["rest"], help="只读 REST API（A15）：接口目录 / 鉴权状态 / 访问地址")
    api_cmd.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本消费")
    api_cmd.set_defaults(func=command_api)

    ab_cmd = sub.add_parser("ab", aliases=["abtest"], help="Prompt/模型 A/B（A22）：固定评测集对比两套配置的准确率/成本/延迟")
    ab_cmd.add_argument("--a", default="default", help="配置 A：预设名或 JSON 对象")
    ab_cmd.add_argument("--b", default="strict", help="配置 B：预设名或 JSON 对象")
    ab_cmd.add_argument("--count", type=int, default=500, help="评测消息条数（默认 500）")
    ab_cmd.add_argument("--seed", type=int, default=20261008, help="随机种子（固定评测集用）")
    ab_cmd.add_argument("--list", action="store_true", dest="list_presets", help="列出内置预设")
    ab_cmd.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本消费")
    ab_cmd.set_defaults(func=command_ab)

    conflict_cmd = sub.add_parser("conflicts", aliases=["conflict"], help="时间冲突检测（A12）：截止时间相近的待办只提醒、不改任务")
    conflict_cmd.add_argument("--window", type=int, default=conflicts.DEFAULT_WINDOW_MINUTES, help="冲突窗口（分钟，默认 30）")
    conflict_cmd.add_argument("--include-done", action="store_true", dest="include_done", help="包含已完成待办")
    conflict_cmd.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本消费")
    conflict_cmd.set_defaults(func=command_conflicts)

    ics_cmd = sub.add_parser("ics", aliases=["calendar"], help="日历导出（A13）：把有明确时间的待办导成 ICS")
    ics_cmd.add_argument("--out", default="", help="输出文件路径（默认 data/calendar.ics）")
    ics_cmd.add_argument("--include-done", action="store_true", dest="include_done", help="包含已完成待办")
    ics_cmd.add_argument("--print", action="store_true", dest="print", help="打印到标准输出，不写文件")
    ics_cmd.add_argument("--json", action="store_true", help="以 JSON 输出摘要")
    ics_cmd.set_defaults(func=command_ics)

    mcp_cmd = sub.add_parser("mcp", aliases=["mcp-serve"], help="只读 MCP 接口（A1）：stdio 给 Cursor / Claude 查询通知、待办、截止、历史消息")
    mcp_cmd.add_argument("--list-tools", action="store_true", dest="list_tools", help="只打印 Tool 定义（调试用），不启动 stdio 会话")
    mcp_cmd.set_defaults(func=command_mcp)

    panel = sub.add_parser("observe", aliases=["panel"], help="可观测面板：过滤率 / 候选量 / 模型调用 / 推送成功率（脱敏）")
    panel.add_argument("--days", type=int, default=7, help="统计最近多少天（默认 7；1 = 按日、7 = 按周、30 = 按月）")
    panel.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本消费")
    panel.set_defaults(func=command_observe)

    tasks = sub.add_parser("tasks", help="查看待办清单和手机访问地址")
    tasks.add_argument("--all", action="store_true", help="包含已完成")
    tasks.add_argument("--limit", type=int, default=30)
    tasks.add_argument(
        "--import-existing", action="store_true", dest="import_existing", help="从历史摘要回填待办"
    )
    tasks.add_argument("--days", type=int, default=7, help="回填最近多少天")
    tasks.add_argument("--reset", action="store_true", help="先清空待办库再回填")
    tasks.set_defaults(func=command_tasks)

    sim = sub.add_parser("simulate", aliases=["sim"], help="假群聊回放整条链路（不联网、不碰真实数据）")
    sim.add_argument("--count", type=int, default=500, help="生成多少条消息")
    sim.add_argument("--seed", type=int, default=20261008, help="随机种子：同参数必得同数据")
    sim.add_argument("--hours", type=float, default=16.0, help="消息铺开多少小时（08:00 起算）")
    sim.add_argument("--fixture", default="", help="改从 JSONL fixture 读消息")
    sim.add_argument("--out", default="", help="把生成的消息写成 JSONL fixture")
    sim.add_argument("--write-only", action="store_true", dest="write_only", help="只写 fixture 不回放")
    sim.add_argument("--dir", default="", help="指定数据目录（默认临时目录，跑完即可删）")
    sim.add_argument("--window", type=int, default=30, help="合并窗口分钟数（默认对齐真实部署）")
    sim.add_argument("--budget", type=int, default=12, help="每日推送额度（0 = 不限）")
    sim.add_argument("--max-items", type=int, default=30, dest="max_items", help="每批最多几条要点")
    sim.add_argument("--min-score", type=int, default=3, dest="min_score", help="入摘要的最低分值")
    sim.add_argument("--quiet-hours", default="", dest="quiet_hours", help="夜间静默时段，如 23:00-07:00")
    sim.set_defaults(func=command_simulate)

    bench = sub.add_parser(
        "benchmark", aliases=["bench"], help="脱敏评测集：召回 / 误报 / 待办 / 截止 / 去重 / 延迟 / 成本"
    )
    bench.add_argument("--count", type=int, default=500, help="评测集条数")
    bench.add_argument("--seed", type=int, default=20261008, help="随机种子：同参数必得同数据")
    bench.add_argument("--hours", type=float, default=16.0, help="消息铺开多少小时（08:00 起算）")
    bench.add_argument("--fixture", default="", help="改从 JSONL fixture 读评测集（需带 _expect 标注）")
    bench.add_argument("--dir", default="", help="指定数据目录（默认临时目录，跑完即可删）")
    bench.add_argument("--window", type=int, default=30, help="合并窗口分钟数（默认对齐真实部署）")
    bench.add_argument("--budget", type=int, default=0, help="每日推送额度（0 = 不限，评测默认不限）")
    bench.add_argument("--max-items", type=int, default=30, dest="max_items", help="每批最多几条要点")
    bench.add_argument("--min-score", type=int, default=3, dest="min_score", help="入摘要的最低分值")
    bench.add_argument("--quiet-hours", default="", dest="quiet_hours", help="夜间静默时段，如 23:00-07:00")
    bench.add_argument("--price-in", type=float, default=0.0, dest="price_in", help="输入单价（元/百万 token）")
    bench.add_argument("--price-out", type=float, default=0.0, dest="price_out", help="输出单价（元/百万 token）")
    bench.add_argument("--json", action="store_true", help="以 JSON 输出全部指标")
    bench.add_argument(
        "--fail-under", type=float, default=0.0, dest="fail_under", help="召回门槛：低于它退出码 1"
    )
    bench.set_defaults(func=command_benchmark)

    catchup = sub.add_parser("catchup", help="从 NapCat 补采最近的历史群消息")
    catchup.set_defaults(func=command_catchup)

    doctor = sub.add_parser("doctor", help="全链路自检")
    doctor.add_argument("--online", action="store_true", help="额外在线校验 QQ 官方机器人凭证")
    doctor.add_argument("--json", action="store_true", help="以 JSON 输出自检结果")
    doctor.set_defaults(func=command_doctor)

    send_test = sub.add_parser("send-test", help="发送测试推送")
    send_test.add_argument("--wait", type=float, default=25, help="等待机器人就绪的秒数")
    send_test.set_defaults(func=command_send_test)

    attach = sub.add_parser("attach-test", help="解析群文件 / 图片，只打印摘要不推送")
    attach.add_argument("path", nargs="?", default="", help="本地文件路径（可选）")
    attach.add_argument("--group", default="", help="群号")
    attach.add_argument("--file-id", default="", help="群文件 file_id")
    attach.add_argument("--busid", default="", help="群文件 busid")
    attach.add_argument("--name", default="", help="文件名（判断类型用）")
    attach.set_defaults(func=command_attach_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "poll"):
        args.poll = 0
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
