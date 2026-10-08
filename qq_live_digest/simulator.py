"""可复现的假群聊生成器（Roadmap A20）。

给测试、Demo 和后续评测集提供**同一份**输入：几个大学班群里一天的消息流——通知、作业、
报名、考试安排、闲聊、广告、图片、文件、跨群重复转发。字段与 OneBot 上报完全一致，
所以既能直接喂给 `DigestService.on_message()`，也能导出成 fixture 反复回放。

两条硬约束：

1. **确定性**：随机只走一个 `seed`，同样的参数永远生成同样的数据——回归对比才有意义。
2. **不碰真东西**：群号 / 人名 / 链接全是编造的，推送通道是内存里的假通道，默认不调用大模型。
   所以生成的数据可以放心出现在 CI、截图和演示视频里。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .config import Settings
from .push import Pusher
from .service import DigestService
from .store import Store
from .timeutil import iso, now_local, parse_iso

LOGGER = logging.getLogger(__name__)

# 全部编造的群号与人名：可以安全贴进 Issue、截图和演示视频（脱敏要求见 docs/SECURITY-BOUNDARY.md）
GROUPS: tuple[dict[str, str], ...] = (
    {"group_id": "900001", "group_name": "2026级软工1班", "kind": "class"},
    {"group_id": "900002", "group_name": "软件工程专业群", "kind": "major"},
    {"group_id": "900003", "group_name": "软工1班班委群", "kind": "committee"},
    {"group_id": "900004", "group_name": "紫荆3号楼通知群", "kind": "life"},
    {"group_id": "900005", "group_name": "数据结构课程交流群", "kind": "course"},
)
# 课程群绝大部分是闲聊：用来演示「安静群」以及整体过滤率
QUIET_GROUP_IDS: tuple[str, ...] = ("900005",)

SENDERS: tuple[str, ...] = (
    "辅导员张老师",
    "教务办公室",
    "学委李同学",
    "班长王同学",
    "后勤中心",
    "同学甲",
    "同学乙",
    "同学丙",
)

CHATTER: tuple[str, ...] = (
    "哈哈哈哈",
    "收到",
    "谢谢老师",
    "好嘞",
    "在吗",
    "签到",
    "+1",
    "笑死",
    "辛苦啦",
    "晚安",
    "这个题怎么做啊",
    "下午有人去图书馆吗",
)

COURSES: tuple[str, ...] = ("数据结构", "操作系统", "计算机网络", "软件工程导论", "大学物理")
BUILDINGS: tuple[str, ...] = ("紫荆3号楼", "紫荆5号楼", "桃李2号楼")


@dataclass(frozen=True)
class Template:
    """一条消息的“剧本”。`kind` 只用于统计与测试断言，不参与判定。"""

    weight: int
    kind: str
    text: str
    event: str = "GROUP_MESSAGE_CREATE"
    group_kind: str = ""


# 真实群聊的粗略比例：闲聊压倒性多数（约 3/4），通知与作业是少数——但正是那少数值得打扰人
TEMPLATES: tuple[Template, ...] = (
    Template(60, "chatter", ""),
    Template(4, "notice", "【学院通知】关于{thing}的通知：请各班于{when}前{action}（{place}）。"),
    Template(3, "homework", "【作业】{course}第{n}次实验报告，{when}前提交到学习通，逾期不予受理。"),
    Template(2, "exam", "【教务处】{course}期中考试安排已公布，请到教务系统查询；时间冲突的同学{when}前联系老师。"),
    Template(2, "urgent", "最后通知：{when}前务必完成{thing}确认，逾期系统自动关闭。"),
    Template(2, "admin", "【后勤】{building}{when}开展{thing}，请同学们提前做好准备。"),
    Template(2, "signup", "【活动报名】{thing}招募志愿者，请有意向的同学{when}前填写报名表。"),
    Template(1, "link", "【选课】{course}选课名单已更新：https://example.edu.cn/notice/{n} ，{when}前完成确认。"),
    Template(1, "image", "[图片] {course}选课时间安排表", event="GROUP_MESSAGE_CREATE"),
    Template(1, "file", "[文件] {course}课程设计模板.docx", event="GROUP_FILE_UPLOAD"),
    Template(3, "repeat", ""),
    Template(6, "chat", "这门课老师讲得挺好的，推荐选", group_kind="course"),
    Template(3, "ad", "【兼职】日结3{n}0元，加微信 abc{nn} 详聊"),
    Template(1, "group_notice", "【班长通知】{when}班会，地点{place}，请务必到场。", group_kind="class"),
)

_THING = ("校园卡办理", "宿舍安全隐患排查", "学期注册", "体质测试", "评奖评优材料收集", "医保信息核对")
_ACTION = ("核对名单", "统计人数", "填写信息", "确认选课", "提交材料", "上报汇总")
_PLACE = ("教务系统", "学院办公室", "学习通", "班级群问卷", "志愿汇 App")


class MemoryPusher(Pusher):
    """内存里的假通道：只记录，绝不发送。"""

    name = "simulator"
    tier = 0

    def __init__(self) -> None:
        super().__init__("memory")
        self.sent: list[tuple[str, str]] = []
        self.skipped: int = 0

    def send(self, title: str, body: str, *, summary: str = "", html: str = "") -> None:
        self.sent.append((title, body))


def _fill(text: str, rng: random.Random) -> str:
    return text.format(
        thing=rng.choice(_THING),
        action=rng.choice(_ACTION),
        action2=rng.choice(_ACTION),
        place=rng.choice(_PLACE),
        course=rng.choice(COURSES),
        building=rng.choice(BUILDINGS),
        when=rng.choice(("今天 18:00", "明天 12:00", "本周五 23:59", "10月16日 17:00", "后天 09:00")),
        n=rng.randint(1, 6),
        nn=rng.randint(100, 999),
    )


def generate(
    count: int = 500,
    *,
    seed: int = 20261008,
    start: dt.datetime | None = None,
    span_hours: float = 16.0,
) -> list[dict[str, Any]]:
    """生成 `count` 条假群消息，时间在 `span_hours` 内均匀铺开。

    同一组参数必然得到同一份数据；`msg_id` 形如 `sim-<seed>-00042`，便于在决策日志里回查。
    默认 08:00 起算、铺 16 小时（到当天 24:00），这样一天的数据不会跨日、额度与静默都落在同一天里。
    """
    if count <= 0:
        raise ValueError("count 必须为正整数")
    rng = random.Random(seed)
    base = start or dt.datetime.combine(now_local().date(), dt.time(8, 0))
    step = dt.timedelta(seconds=(max(0.0, span_hours) * 3600) / count)
    weights = [item.weight for item in TEMPLATES]
    records: list[dict[str, Any]] = []
    for index in range(count):
        template = rng.choices(TEMPLATES, weights=weights, k=1)[0]
        stamp = base + step * index
        content = _fill(template.text, rng) if template.text else rng.choice(CHATTER)
        if template.kind == "repeat" and records:
            # 同一条通知被两个群连着转发：用来演示跨群去重
            previous = records[-1]
            content = previous["content"]
            group = rng.choice([item for item in GROUPS if item["group_id"] != previous["group_id"]])
            sender = previous["sender_name"]
        else:
            candidates = [
                item for item in GROUPS if not template.group_kind or item["kind"] == template.group_kind
            ]
            group = rng.choice(candidates or list(GROUPS))
            sender = rng.choice(SENDERS)
        if template.kind == "chatter" and group["group_id"] not in QUIET_GROUP_IDS:
            group = next(item for item in GROUPS if item["group_id"] == rng.choice(QUIET_GROUP_IDS))
        records.append(
            {
                "msg_id": f"sim-{seed}-{index:05d}",
                "source": "simulator",
                "event": template.event,
                "group_id": group["group_id"],
                "group_name": group["group_name"],
                "sender_id": f"u{rng.randint(1, 40):03d}",
                "sender_name": sender,
                "ts": iso(stamp),
                "received_at": iso(stamp),
                "content": content,
                "source_text": "",
            }
        )
    return records


def write_fixture(path: Path | str, records: Iterable[dict[str, Any]]) -> int:
    """把消息写成 JSONL fixture（一行一条），便于反复回放与进测试。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with target.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1
    return written


def read_fixture(path: Path | str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def simulator_settings(
    data_dir: Path | str,
    *,
    quiet_hours: str = "",
    window_minutes: int = 30,
    daily_budget: int = 0,
    max_items: int = 30,
    min_score: int = 3,
) -> Settings:
    """一套自洽的演示 / 测试配置：假群白名单、离线、无真实通道、无大模型。

    默认值对齐真实部署（`window_minutes=30` / `min_score=3` / `max_items=30`），
    只把每日额度设为不限——因为额度属于「部署时的取舍」，评估链路时不该混进来。
    """
    return Settings(
        group_whitelist=tuple(item["group_id"] for item in GROUPS),
        group_aliases={item["group_id"]: item["group_name"] for item in GROUPS},
        quiet_groups=QUIET_GROUP_IDS,
        data_dir=Path(data_dir),
        window_minutes=int(window_minutes),
        min_score=int(min_score),
        max_items=int(max_items),
        max_batch=200,
        quiet_hours=quiet_hours,
        push_daily_budget=int(daily_budget),
        delivery_retry_seconds=0,
        llm_enabled=False,
        dashscope_api_key="",
        html_push=False,
        attachments_enabled=False,
        catchup_enabled=False,
        web_enabled=False,
        onebot_enabled=False,
        official_bot_enabled=False,
        weekly_review_enabled=False,
        deadline_reminders_enabled=False,
        candidate_push_enabled=False,
    )


def build_service(
    data_dir: Path | str,
    *,
    quiet_hours: str = "",
    window_minutes: int = 30,
    daily_budget: int = 0,
    max_items: int = 30,
    min_score: int = 3,
    pusher: MemoryPusher | None = None,
    logger: logging.Logger | None = None,
) -> tuple[DigestService, MemoryPusher]:
    settings = simulator_settings(
        data_dir,
        quiet_hours=quiet_hours,
        window_minutes=window_minutes,
        daily_budget=daily_budget,
        max_items=max_items,
        min_score=min_score,
    )
    channel = pusher or MemoryPusher()
    service = DigestService(
        settings,
        store=Store(Path(data_dir) / "digest.sqlite3"),
        logger=logger or LOGGER,
        pushers=[channel],
    )
    return service, channel


@dataclass
class FunnelReport:
    """「500 条消息最后剩下什么」的漏斗。"""

    messages: int = 0
    pushed: int = 0          # 真正打扰了用户几次
    push_items: int = 0      # 推送里一共几条要点
    tasks: int = 0           # 落进待办的事项
    deferred: int = 0        # 被静默 / 额度 / 失败推迟、还没最终结论的条数
    decisions: dict[str, int] = field(default_factory=dict)
    first_title: str = ""
    first_body: str = ""

    @property
    def filtered(self) -> int:
        return int(self.decisions.get("filtered") or 0) + int(self.decisions.get("rejected") or 0)

    def summary_lines(self) -> list[str]:
        lines = [
            f"消息 {self.messages} 条 → 推送 {self.pushed} 次（{self.push_items} 条要点）→ 待办 {self.tasks} 项",
            f"过滤掉 {self.filtered} 条（未命中 / 入口拒绝），命中率 "
            f"{0.0 if not self.messages else (self.messages - self.filtered) * 100.0 / self.messages:.1f}%",
        ]
        if self.decisions:
            from . import decisions as _decisions

            lines.append("决策分布：" + _decisions.summarise_counts(self.decisions))
        if self.deferred:
            lines.append(f"另有 {self.deferred} 条「延后未决」，会在下个窗口重试")
        return lines


def replay(
    records: list[dict[str, Any]],
    *,
    data_dir: Path | str,
    quiet_hours: str = "",
    window_minutes: int = 30,
    daily_budget: int = 0,
    max_items: int = 30,
    min_score: int = 3,
    step_minutes: int = 5,
    service: DigestService | None = None,
    channel: MemoryPusher | None = None,
    logger: logging.Logger | None = None,
) -> FunnelReport:
    """把一批消息喂进真实链路，并按时间推进调度时钟，最后统计漏斗。

    走的是和生产完全相同的代码路径（入库 → 判定 → 摘要 → 投递 → 决策日志），
    差别只有两处：通道是内存假通道，大模型关闭时自动回退本地规则。
    """
    if service is None or channel is None:
        built_service, built_channel = build_service(
            data_dir,
            quiet_hours=quiet_hours,
            window_minutes=window_minutes,
            daily_budget=daily_budget,
            max_items=max_items,
            min_score=min_score,
            logger=logger,
        )
        service = service or built_service
        channel = channel or built_channel
    ordered = sorted(records, key=lambda item: str(item.get("received_at") or ""))
    moments = [parse_iso(item.get("received_at")) for item in ordered]
    moments = [item for item in moments if isinstance(item, dt.datetime)]
    if not moments:
        return FunnelReport()
    step = dt.timedelta(minutes=max(1, int(step_minutes)))
    # 关键：按时间顺序「边到达边 tick」，否则所有消息会挤在同一个窗口里被一次吃掉，
    # 滚动窗口、批内去重和每批条数上限都失去意义（漏斗就不真实了）。
    cursor = moments[0]
    for record, moment in zip(ordered, [parse_iso(item.get("received_at")) for item in ordered]):
        while isinstance(moment, dt.datetime) and cursor + step <= moment:
            service.tick(now=cursor)
            cursor += step
        service.on_message(record)
    end = moments[-1] + dt.timedelta(minutes=service.settings.window_minutes * 3)
    while cursor <= end:
        service.tick(now=cursor)
        cursor += step
    service.tick(now=end + step)

    decisions_yearly = service.store.decision_counts(hours=24 * 365)
    counts = service.store.counts()
    stats = service.store.task_stats()
    tasks = int(
        stats.get("total")
        or sum(
            int(stats.get(key) or 0)
            for key in ("candidate", "open", "done", "dismissed", "expired")
        )
    )
    report = FunnelReport(
        messages=len(records),
        pushed=len(channel.sent),
        push_items=int(decisions_yearly.get("pushed") or 0),
        tasks=tasks,
        deferred=int(counts.get("decisions_deferred") or 0),
        decisions=decisions_yearly,
    )
    if channel.sent:
        report.first_title, report.first_body = channel.sent[0]
    LOGGER.debug("模拟完成：%s", " / ".join(report.summary_lines()))
    return report
