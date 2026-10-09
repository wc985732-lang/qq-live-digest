"""滚动窗口调度：紧急立即推、普通合并推、无重点不推，失败投递自动重试。"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import logging
import threading
from typing import Any

from . import confidence
from . import decisions
from . import providers
from .bot import BotRunner
from .attachments import Attachment, AttachmentWorker, cleanup_files
from .catchup import NapCatClient, backfill
from .config import Settings, ensure_dirs
from .push import PushManager, Pusher, build_pushers
from .receiver import OneBotReceiver
from .store import Store
from .summarizer import Digest, analyse_records, build_digest, classify_task_item, finalize_digest, html_push_text, payload_to_item, urgent_analyses
from .summarizer import is_task_eligible
from .timeutil import iso, now_local, parse_iso
from .weburl import build_web_url
from .webapp import TaskWebServer

LOGGER = logging.getLogger(__name__)

PRUNE_INTERVAL_SECONDS = 6 * 3600
FORCE_FLUSH_MULTIPLIER = 6
CATCHUP_RETRY_SECONDS = 5 * 60

# 推送闸门给出的原因码 → 给人看的话术（决策日志里用）
GATE_REASON_TEXT = {
    "quiet_hours": "夜间静默时段",
    "daily_budget": "当日推送额度已用完",
}


def _candidate_reason(task: dict[str, Any]) -> str:
    """候选卡片的「为什么」：优先用入库的触发规则，退回分类理由（A7）。"""
    raw = str(task.get("candidate_detail") or "")
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    why = confidence.describe_triggers(parsed.get("triggers") or [], limit=3)
    return why or str(parsed.get("reason") or "")


class DigestService:
    def __init__(
        self,
        settings: Settings,
        *,
        store: Store | None = None,
        logger: logging.Logger | None = None,
        bot: Any = None,
        receiver: OneBotReceiver | None = None,
        pushers: list[Pusher] | None = None,
    ) -> None:
        ensure_dirs(settings)
        self.settings = settings
        self.logger = logger or LOGGER
        self.store = store or Store(
            settings.data_dir / "digest.sqlite3",
            retention_days=settings.message_retention_days,
        )
        # 模型用量落库（A5）：Provider 层只在挂了记录器时才写库，测试与库外调用不受影响。
        self._llm_recorder = self.store.add_llm_call
        providers.set_call_recorder(self._llm_recorder)
        self._bot = bot
        self._receiver = receiver
        self._pushers = pushers
        self._pushers_locked = pushers is not None
        self._push_manager: PushManager | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.started_at: dt.datetime | None = None
        self.last_tick_at: dt.datetime | None = None
        self.last_catchup_at: dt.datetime | None = None
        self.last_catchup_attempt_at: dt.datetime | None = None
        self._catchup_thread: threading.Thread | None = None
        self._attachment_worker: AttachmentWorker | None = None
        self._web: TaskWebServer | None = None

        # 可靠性与可观测性状态
        self.last_llm_error = ""
        self.last_llm_error_at: dt.datetime | None = None
        self.last_defer_reason = ""
        self.last_defer_at: dt.datetime | None = None
        self.last_push_at: dt.datetime | None = None

    # ------------------------------------------------------------- 组件装配
    @property
    def attachment_worker(self) -> AttachmentWorker:
        if self._attachment_worker is None:
            self._attachment_worker = AttachmentWorker(
                self.settings,
                self._on_attachment_record,
                logger=self.logger,
                napcat_call=self._napcat_call,
            )
        return self._attachment_worker

    def _napcat_call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        client = NapCatClient(
            self.settings.napcat_api_url,
            self.settings.napcat_api_token,
            self.settings.http_timeout,
        )
        return client.call(action, payload)

    def _submit_attachment(self, attachment: Attachment) -> bool:
        """收到群文件 / 群图片事件，交给后台队列慢慢处理。"""
        if not self.settings.attachments_enabled or attachment is None:
            return False
        return bool(self.attachment_worker.submit(attachment))

    def _on_attachment_record(self, record: dict[str, Any]) -> bool:
        """附件解析完成后的记录，走普通消息流程。"""
        return bool(self.on_message(record))

    @property
    def bot(self) -> Any:
        if self._bot is None:
            self._bot = BotRunner(
                self.settings,
                self.on_message,
                data_dir=self.settings.data_dir,
                logger=self.logger,
            )
        return self._bot

    @property
    def receiver(self) -> OneBotReceiver:
        if self._receiver is None:
            self._receiver = OneBotReceiver(
                self.settings,
                self.on_message,
                self._submit_attachment,
                self.logger,
                health_provider=self.health_payload,
            )
        return self._receiver

    def _ensure_push_manager(self) -> PushManager:
        api = getattr(self._bot, "api", None)
        loop = getattr(self._bot, "loop", None)
        needs_build = self._push_manager is None or self._pushers is None
        if not needs_build and not self._pushers_locked and api is not None:
            needs_build = not any(pusher.name == "qq-bot" for pusher in self._pushers or [])
        if needs_build:
            if not self._pushers_locked:
                self._pushers = build_pushers(self.settings, api=api, loop=loop)
            self._push_manager = PushManager(self.store, self.settings, list(self._pushers or []), self.logger)
        assert self._push_manager is not None
        return self._push_manager

    def push_manager(self) -> PushManager:
        """公开访问器，供 doctor / send-test 复用同一套通道与去重表。"""
        return self._ensure_push_manager()

    # --------------------------------------------------------------- 消息入口
    def on_message(self, record: dict[str, Any]) -> bool:
        msg_id = str(record.get("msg_id") or "").strip()
        if not msg_id:
            self.logger.warning("忽略没有 msg_id 的消息：%s", str(record)[:200])
            self._record_decision(
                record,
                stage=decisions.STAGE_INTAKE,
                outcome=decisions.REJECTED,
                reason="消息缺少 msg_id，无法去重",
            )
            return False
        group_id = str(record.get("group_id") or "")
        if group_id and not self.settings.accepts_group(group_id):
            self.logger.debug("忽略白名单外的群：%s", group_id)
            self._record_intake_rejection(record, group_id, "群不在 QQ_DIGEST_GROUPS 白名单内")
            return False
        group_name = str(record.get("group_name") or "") or self.settings.group_name(group_id)
        try:
            inserted = self.store.insert_message(
                msg_id=msg_id,
                group_id=group_id,
                content=str(record.get("content") or ""),
                source_text=str(record.get("source_text") or ""),
                ts=record.get("ts"),
                received_at=record.get("received_at") or now_local(),
                source=str(record.get("source") or "qqbot"),
                event=str(record.get("event") or ""),
                sender_id=str(record.get("sender_id") or ""),
                sender_name=str(record.get("sender_name") or ""),
                group_name=group_name,
            )
        except Exception:  # noqa: BLE001 - 入库失败不能让机器人线程崩掉
            self.logger.exception("消息入库失败：%s", msg_id)
            return False
        if inserted:
            self.logger.info(
                "收到消息 group=%s sender=%s id=%s text=%s",
                group_name,
                record.get("sender_name") or record.get("sender_id") or "?",
                msg_id,
                str(record.get("content") or "")[:60].replace("\n", " "),
            )
        else:
            self._record_decision(
                record,
                stage=decisions.STAGE_INTAKE,
                outcome=decisions.DUPLICATE,
                reason="msg_id 重复：这条消息之前已经入库处理过",
            )
        return inserted

    # --------------------------------------------------------------- 决策日志
    def _record_decision(
        self,
        record: dict[str, Any],
        *,
        stage: str,
        outcome: str,
        reason: str,
        digest_id: int = 0,
        when: dt.datetime | None = None,
    ) -> None:
        """写一条决策轨迹。这是旁路观测：写不进去只降级 debug，绝不拖垮消息链路。"""
        try:
            self.store.record_decisions(
                [
                    {
                        "msg_id": str(record.get("msg_id") or ""),
                        "group_id": str(record.get("group_id") or ""),
                        "digest_id": int(digest_id or 0),
                        "stage": stage,
                        "outcome": outcome,
                        "reason": reason,
                        "created_at": iso(when or now_local()),
                    }
                ]
            )
        except Exception:  # noqa: BLE001
            self.logger.debug("决策日志写入失败：%s", record.get("msg_id"), exc_info=True)

    def _record_intake_rejection(self, record: dict[str, Any], group_id: str, reason: str) -> None:
        """白名单外的群可能一直在刷，不能来一条记一行：每个群每天只记第一条。"""
        key = f"reject_log:{group_id}:{now_local():%Y%m%d}"
        if self._bump_meta_counter(key) > 1:
            return
        self._record_decision(
            record,
            stage=decisions.STAGE_INTAKE,
            outcome=decisions.REJECTED,
            reason=f"{reason}（该群今天只记这一条，其余同类消息不再重复记录）",
        )

    def _flush_decisions(self, digest: Digest, *, delivered: bool) -> None:
        """把本批决策轨迹落库：未命中/去重/截断照抄，「命中候选」回填最终结果。"""
        rows = [dict(row) for row in (digest.decisions or [])]
        if not rows:
            return
        for row in rows:
            if str(row.get("outcome") or "") != decisions.PENDING:
                continue
            row["digest_id"] = int(digest.id or 0)
            base = str(row.get("reason") or "命中候选")
            if delivered:
                row["outcome"] = decisions.PUSHED
                row["reason"] = f"{base}，已推送"
            else:
                row["outcome"] = decisions.HELD
                row["reason"] = f"{base}，本次未投出（暂无可用通道、全部失败或稍后重试）"
        try:
            self.store.record_decisions(rows)
        except Exception:  # noqa: BLE001
            self.logger.debug("决策日志批量写入失败（digest=%s）", digest.id, exc_info=True)

    # --------------------------------------------------------------- 生命周期
    # ------------------------------------------------------- 限流与降级状态
    def _meta_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.store.meta_get(key, str(default)) or default)
        except (TypeError, ValueError):
            return default

    def _bump_meta_counter(self, key: str, amount: int = 1) -> int:
        value = self._meta_int(key) + int(amount)
        self.store.meta_set(key, str(value))
        return value

    def _llm_defer_decision(self, stamp: dt.datetime) -> str:
        """返回 defer（本批留到下次再试）或 fallback（回退本地规则照常推）。"""
        if int(self.settings.llm_defer_max_attempts) <= 0:
            return "fallback"
        started = parse_iso(self.store.meta_get("llm_defer:started", ""))
        count = self._meta_int("llm_defer:count")
        window = dt.timedelta(minutes=max(1, int(self.settings.llm_defer_window_minutes)))
        if not isinstance(started, dt.datetime) or (stamp - started) > window:
            self.store.meta_set("llm_defer:started", iso(stamp))
            self.store.meta_set("llm_defer:count", "1")
            return "defer"
        if count >= int(self.settings.llm_defer_max_attempts):
            return "fallback"
        self.store.meta_set("llm_defer:count", str(count + 1))
        return "defer"

    def _llm_defer_reset(self) -> None:
        if self._meta_int("llm_defer:count"):
            self.store.meta_set("llm_defer:started", "")
            self.store.meta_set("llm_defer:count", "0")
        self.last_defer_reason = ""

    def _push_sent_today(self, stamp: dt.datetime) -> int:
        return self._meta_int(f"push_budget:{stamp:%Y-%m-%d}")

    def _note_push_sent(self, stamp: dt.datetime, count: int = 1) -> None:
        if count > 0:
            self._bump_meta_counter(f"push_budget:{stamp:%Y-%m-%d}", count)
            self.last_push_at = stamp

    def _event_splits(self) -> tuple[str, ...]:
        """A11：用户标记过「不要自动合并」的事件键；没开事件聚合时返回空。"""
        if not bool(getattr(self.settings, "event_merge", False)):
            return ()
        return self.store.event_split_keys()

    def _push_gate(self, stamp: dt.datetime, *, urgent: bool = False, ignore_quiet: bool = False) -> tuple[bool, str]:
        """非紧急推送的统一闸门：先看夜间静默，再看当日预算。"""
        if urgent:
            return True, ""
        if not ignore_quiet and self.settings.in_quiet_hours(stamp):
            return False, "quiet_hours"
        budget = int(self.settings.push_daily_budget or 0)
        if budget > 0 and self._push_sent_today(stamp) >= budget:
            return False, "daily_budget"
        return True, ""

    def _hold_batch(self, stamp: dt.datetime, count: int, reason: str) -> None:
        self.last_defer_reason = reason
        self.last_defer_at = stamp
        self.logger.info("本批 %d 条消息暂不推送（%s），留到下次窗口。", count, reason)

    def _record_deferred(
        self,
        items: list[dict[str, Any]],
        reason: str,
        *,
        msg_ids: set[str] | None = None,
        stage: str = decisions.STAGE_PUBLISH,
        when: dt.datetime | None = None,
    ) -> int:
        """记「本该推送，但这次被推迟」的过程行（按 msg_id 就地更新，不涨行）。

        补上 A33 决策日志此前整段空白的那一块：被夜间静默 / 当日额度 / 大模型失败挡住的消息
        以前不留任何痕迹，而夜间恰恰是最常见的场景——于是日志看起来像"什么都没发生"。
        `items` 既可以是分析结果，也可以是 digest 里的候选行，两边字段是同一套命名。
        """
        text = GATE_REASON_TEXT.get(reason, reason)
        rows: list[dict[str, Any]] = []
        for item in items or []:
            msg_id = str(item.get("msg_id") or "")
            if not msg_id or (msg_ids is not None and msg_id not in msg_ids):
                continue
            rows.append(
                {
                    "msg_id": msg_id,
                    "group_id": str(item.get("group_id") or ""),
                    "stage": str(item.get("stage") or stage),
                    "reason": f"延后：{text}",
                    "score": int(item.get("score") or 0),
                    "min_score": int(item.get("min_score") or self.settings.min_score or 0),
                    "category": str(item.get("category") or ""),
                    "rule_hits": item.get("rule_hits") or decisions.rule_hits(item),
                    "created_at": iso(when or now_local()),
                }
            )
        if not rows:
            return 0
        try:
            return int(self.store.record_deferred(rows))
        except Exception:  # noqa: BLE001 - 旁路观测，写不进去也绝不拖垮消息链路
            self.logger.debug("延后决策写入失败", exc_info=True)
            return 0

    # --------------------------------------------------------------- 生命周期
    def start(self, *, start_bot: bool = True) -> None:
        if self.started_at is not None:
            return
        self.started_at = now_local()
        self._stop.clear()
        if start_bot and self.settings.official_bot_enabled and self.settings.appid and self.settings.secret:
            started = self.bot.start()
            self.logger.info("官方机器人启动：%s", started)
        elif start_bot and self.settings.official_bot_enabled:
            self.logger.error("缺少 AppID/AppSecret，官方机器人未启动。")
        elif start_bot:
            self.logger.info("官方 QQ 机器人已禁用，仅使用 OneBot/NapCat。")
        if self.settings.onebot_enabled:
            try:
                self.receiver.start()
            except OSError as error:
                self.logger.error("OneBot 接收器启动失败：%s", error)
        if self.settings.attachments_enabled:
            self.attachment_worker.start()
            self.logger.info(
                "附件解析已启用：单文件上限 %dMB、每小时最多 %d 个、图片OCR=%s（%s）、扫描PDF限%d页。",
                self.settings.attachment_max_mb,
                self.settings.attachment_max_per_hour,
                "开" if self.settings.vision_enabled else "关",
                self.settings.vision_model,
                self.settings.pdf_ocr_max_pages,
            )
        if self.settings.web_enabled:
            self._web = TaskWebServer(
                self.settings,
                self.store,
                logger=self.logger,
                meta_provider=self._web_meta,
            )
            self._web.start()
        if self.settings.catchup_enabled:
            self._schedule_catchup()
        supervisor = threading.Thread(target=self._supervise, name="supervisor", daemon=True)
        supervisor.start()
        self._threads.append(supervisor)
        self.logger.info("服务已启动，窗口 %d 分钟，轮询 %d 秒。", self.settings.window_minutes, self.settings.poll_seconds)

    def _supervise(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            self._stop.wait(backoff)
            if self._stop.is_set():
                break
            if not self._bot or self._pushers_locked:
                continue
            if (
                self.settings.official_bot_enabled
                and self.settings.appid
                and self.settings.secret
                and not self._bot.is_alive
            ):
                self.logger.warning("检测到 QQ 机器人未运行（%s），尝试重启。", self._bot.last_error or "未知原因")
                started = self._bot.start()
                backoff = 5.0 if started else min(backoff * 2, 300)
            else:
                backoff = 5.0

    # --------------------------------------------------------------- 历史补采
    def _schedule_catchup(self) -> None:
        if not self.settings.catchup_enabled:
            return
        if self._catchup_thread is not None and self._catchup_thread.is_alive():
            return
        self._catchup_thread = threading.Thread(target=self.run_catchup, name="catchup", daemon=True)
        self._catchup_thread.start()

    def run_catchup(self, *, force: bool = False, now: dt.datetime | None = None) -> int:
        stamp = now or now_local()
        self.last_catchup_attempt_at = stamp
        stats: dict[str, int] = {}
        try:
            inserted = backfill(
                self.settings,
                self.store,
                self.logger,
                now=stamp,
                force=force,
                stats=stats,
                attachment_sink=self._submit_attachment,
            )
        except Exception:  # noqa: BLE001 - 补采失败不应影响常驻服务
            self.logger.exception("历史补采异常")
            return 0
        groups = int(stats.get("groups", 0) or 0)
        ok_groups = int(stats.get("ok_groups", 0) or 0)
        if groups and ok_groups == 0:
            self.logger.warning(
                "历史补采全部失败（%d 个群），%d 分钟后自动重试。",
                groups,
                CATCHUP_RETRY_SECONDS // 60,
            )
            return 0
        self.last_catchup_at = stamp
        return inserted

    def _maybe_catchup(self, stamp: dt.datetime) -> None:
        if not self.settings.catchup_enabled:
            return
        interval = max(5, int(self.settings.catchup_interval_minutes)) * 60
        if self.last_catchup_at is not None:
            if (stamp - self.last_catchup_at).total_seconds() < interval:
                return
        elif self.last_catchup_attempt_at is not None:
            if (stamp - self.last_catchup_attempt_at).total_seconds() < CATCHUP_RETRY_SECONDS:
                return
        self._schedule_catchup()

    def run_forever(self, poll_seconds: int | None = None) -> None:
        interval = max(5, poll_seconds or self.settings.poll_seconds)
        self.start()
        try:
            while not self._stop.wait(interval):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 - 调度循环必须存活
                    self.logger.exception("调度轮询失败")
        except KeyboardInterrupt:
            self.logger.info("收到中断信号，正在停止。")
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        # 只清除自己挂的记录器，避免把同时存在的另一个服务实例的记录器抹掉。
        if providers.get_call_recorder() is self._llm_recorder:
            providers.set_call_recorder(None)
        if self._catchup_thread is not None and self._catchup_thread.is_alive():
            self._catchup_thread.join(timeout=5)
        if self._attachment_worker is not None:
            self._attachment_worker.stop()
        if self._web is not None:
            self._web.stop()
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads.clear()
        if self._bot is not None:
            try:
                self._bot.stop()
            except Exception:  # noqa: BLE001
                self.logger.exception("停止机器人失败")
        if self._receiver is not None:
            try:
                self._receiver.stop()
            except Exception:  # noqa: BLE001
                self.logger.exception("停止 OneBot 接收器失败")
        self.started_at = None
        self.logger.info("服务已停止。")

    # ------------------------------------------------------------------ 调度
    def _maybe_deadline_reminders(self, stamp: dt.datetime) -> None:
        """到点后从 tasks 表读取开放任务；每任务每天最多提醒一次。"""
        if not self.settings.deadline_reminders_enabled:
            return
        evening_moment = self._clock_moment(stamp, self.settings.deadline_evening)
        for kind, clock in (
            ("morning", self.settings.deadline_morning),
            ("evening", self.settings.deadline_evening),
        ):
            moment = self._clock_moment(stamp, clock)
            if moment is None or stamp < moment:
                continue
            key = f"deadline_reminder:{kind}:{stamp:%Y-%m-%d}"
            if self.store.meta_get(key):
                continue
            if kind == "morning" and evening_moment is not None and stamp >= evening_moment:
                # 晚上才开机：早上那次已错过，直接由晚上的提醒覆盖，不补发。
                self.store.meta_set(key, iso(stamp))
                continue
            items = self._deadline_items(stamp, kind)
            if not items:
                self.store.meta_set(key, iso(stamp))
                continue
            # 没有可用通道时不要占位，否则这一天的提醒会被永久吞掉。
            manager = self._ensure_push_manager()
            if not manager.has_channels:
                self.logger.warning("截止提醒无可用推送通道，保留待下次重试。")
                continue
            allowed, reason = self._push_gate(stamp, ignore_quiet=True)
            if not allowed:
                self.logger.info("截止提醒暂不推送（%s），保留待下次重试。", reason)
                continue
            digest = self._deadline_digest(kind, stamp, items)
            delivered = self._publish(
                digest,
                when=stamp,
                push_dedupe_key=f"deadline:{kind}:{stamp:%Y-%m-%d}",
            )
            if not delivered:
                self.logger.warning("截止提醒投递失败，保留待下次重试。")
                continue
            self.store.meta_set(key, iso(stamp))
            for item in items:
                task_id = int(item.get("task_id") or 0)
                if task_id:
                    self.store.mark_task_reminded(
                        task_id,
                        detail={"kind": kind, "deadline": str(item.get("deadline_dt") or "")},
                        when=stamp,
                    )
            self.logger.info("已推送%s，涵盖 %d 条截止事项。", digest.title, len(items))

    @staticmethod
    def _clock_moment(stamp: dt.datetime, clock: str) -> dt.datetime | None:
        try:
            hour, minute = (int(part) for part in str(clock).split(":", 1))
        except (TypeError, ValueError):
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return stamp.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def _deadline_items(self, stamp: dt.datetime, kind: str = "morning") -> list[dict[str, Any]]:
        """早上盯今天和逾期，晚上盯明天；忽略已完成、已忽略和待确认任务。"""
        tomorrow = stamp.date() + dt.timedelta(days=1)
        items: list[dict[str, Any]] = []
        for task in self.store.list_open_tasks(limit=2000):
            deadline = parse_iso(task.get("deadline"))
            if not isinstance(deadline, dt.datetime):
                continue
            if kind == "morning" and deadline.date() > stamp.date():
                continue
            if kind == "evening" and deadline.date() != tomorrow:
                continue
            last_reminded = parse_iso(task.get("last_reminded_at"))
            if last_reminded and last_reminded.date() == stamp.date():
                continue
            snooze_until = parse_iso(task.get("snooze_until"))
            if isinstance(snooze_until, dt.datetime) and snooze_until > stamp:
                continue  # 用户点了稍后提醒，静默到指定时间
            groups = task.get("groups") or []
            entry = {
                "category": task.get("category") or "action",
                "importance": task.get("importance") or 4,
                "summary": task.get("summary") or "",
                "action": task.get("action") or "",
                "evidence": task.get("evidence") or task.get("summary") or "",
                "deadline": iso(deadline),
                "group": groups[0] if groups else "",
                "group_id": "",
                "sender": task.get("sender") or "",
                "text": task.get("evidence") or task.get("summary") or "",
                "msg_id": task.get("task_key") or "",
                "created_at": task.get("created_at") or "",
            }
            item = payload_to_item(entry)
            item["task_id"] = int(task.get("id") or 0)
            item["deadline_dt"] = deadline
            item["source"] = str(task.get("source") or "qq_message")
            items.append(item)
        items.sort(key=lambda item: item.get("deadline_dt") or dt.datetime.max)
        return items[:DEADLINE_MAX_ITEMS]

    def _deadline_digest(self, kind: str, stamp: dt.datetime, items: list[dict[str, Any]]) -> Digest:
        groups: list[str] = []
        for item in items:
            name = str(item.get("group_name") or "")
            if name and name not in groups:
                groups.append(name)
        digest = Digest(
            kind=kind,
            window_start=stamp,
            window_end=stamp,
            items=items,
            message_ids=[],
            message_count=len(items),
            groups=groups,
        )
        return finalize_digest(digest, self.settings)

    def _task_url(self, task_id: int) -> str:
        base = self.store.meta_get("web_base_url", "").strip().rstrip("/")
        token = self.settings.web_token or self.store.meta_get("web_token", "")
        return build_web_url(self.settings, token=token, task_id=task_id, base_url=base)

    def _maybe_candidate_reminders(self, stamp: dt.datetime) -> int:
        if not self.settings.candidate_push_enabled or self.settings.candidate_push_max_per_tick <= 0:
            return 0
        daily_key = f"candidate_push:{stamp:%Y-%m-%d}"
        try:
            daily_sent = int(self.store.meta_get(daily_key, "0") or 0)
        except ValueError:
            daily_sent = 0
        if daily_sent >= self.settings.candidate_push_max_per_day:
            return 0
        manager = self._ensure_push_manager()
        if not manager.has_channels:
            return 0
        allowed, reason = self._push_gate(stamp)
        if not allowed:
            self.logger.info("候选确认提醒暂不推送（%s）。", reason)
            return 0
        sent = 0
        for task in self.store.list_tasks(statuses=("candidate",), limit=30):
            confidence_value = float(task.get("confidence") or 0.0)
            if confidence_value < self.settings.candidate_min_confidence:
                continue
            snooze_until = parse_iso(task.get("snooze_until"))
            if isinstance(snooze_until, dt.datetime) and snooze_until > stamp:
                continue  # 用户点了稍后提醒
            last = parse_iso(task.get("last_reminded_at"))
            if last and (stamp - last).total_seconds() < self.settings.candidate_reminder_hours * 3600:
                continue
            task_id = int(task.get("id") or 0)
            link = self._task_url(task_id)
            summary = str(task.get("summary") or "待确认事项")
            evidence = str(task.get("evidence") or summary)
            deadline = str(task.get("deadline") or "")[:16].replace("T", " ") or "未识别"
            groups = "、".join(task.get("groups") or []) or "来源群未知"
            why = _candidate_reason(task)
            reasons = [
                f"可能要做：{summary}",
                f"来源：{groups}",
                f"推测截止：{deadline}",
                f"依据：{evidence}",
                f"判断把握：{confidence.confidence_text(confidence_value)}",
            ]
            if why:
                reasons.append(f"为什么：{why}")
            reasons.append(f"打开待办台确认：{link}")
            body = "\n".join(reasons)
            why_html = (
                f'<div style="font-size:12px;color:#9ca3af;margin-top:2px">{html.escape(why)}</div>'
                if why
                else ""
            )
            html_body = (
                '<div style="padding:12px;border-radius:10px;'
                'background:linear-gradient(135deg,#fff7ed,#eef2ff);">'
                '<div style="font-size:12px;color:#c2410c;font-weight:600">待确认</div>'
                f'<div style="font-size:15px;color:#111827;font-weight:600;margin-top:4px">{html.escape(summary)}</div>'
                f'<div style="font-size:12px;color:#6b7280;margin-top:6px">{html.escape(evidence[:100])}</div>'
                f'<div style="font-size:12px;color:#6b7280;margin-top:4px">{html.escape(groups)} · 推测截止 {html.escape(deadline)}</div>'
                f'<div style="font-size:12px;color:#6b7280;margin-top:4px">'
                f"判断把握：{html.escape(confidence.confidence_text(confidence_value))}</div>"
                f"{why_html}"
                f'<div style="margin-top:8px"><a href="{html.escape(link)}" style="color:#4f46e5">在待办台确认或忽略</a></div>'
                "</div>"
            )
            outcomes = manager.send_card(
                "有一条通知可能需要处理",
                body,
                dedupe_key=f"candidate:{task_id}:{stamp:%Y%m%d}",
                summary=f"待确认：{summary}",
                html=html_body,
            )
            delivered_now = any(item.ok and not item.skipped for item in outcomes)
            already_delivered = any(item.skipped and item.status == "sent" for item in outcomes)
            if delivered_now or already_delivered:
                self.store.mark_task_reminded(
                    task_id,
                    detail={"kind": "candidate_confirmation", "confidence": confidence_value},
                    when=stamp,
                )
                if delivered_now:
                    self._note_push_sent(stamp)
                sent += 1
                if sent >= self.settings.candidate_push_max_per_tick:
                    break
        if sent:
            self.store.meta_set(daily_key, str(daily_sent + sent))
        return sent

    def _maybe_weekly_review(self, stamp: dt.datetime) -> None:
        if not self.settings.weekly_review_enabled or stamp.weekday() != self.settings.weekly_review_weekday:
            return
        moment = self._clock_moment(stamp, self.settings.weekly_review_time)
        if moment is None or stamp < moment:
            return
        key = f"weekly_review:{stamp:%G-%V}"
        if self.store.meta_get(key):
            return
        start = (stamp - dt.timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        metrics = self.store.task_metrics(start=iso(start), end=iso(stamp))
        lines = [
            f"新增候选 {metrics['candidates']} 条，确认 {metrics['confirmed']} 条，忽略 {metrics['dismissed']} 条",
            f"完成 {metrics['completions']} 条，提醒 {metrics['reminders']} 次，提醒后完成 {metrics['completions_after_reminder']} 条",
            f"确认率 {metrics['confirmation_rate']}%，忽略率 {metrics['dismissal_rate']}%，逾期未完成 {metrics['overdue']} 条",
        ]
        insights = self.store.correction_insights(days=7, min_group_hits=2)
        lines.extend(f"纠错提示：{text}" for text in insights[:3])
        body = "\n".join(lines)
        html_body = (
            '<div style="padding:2px 0">'
            '<div style="font-size:13px;color:#5f6368;margin-bottom:6px">过去 7 天</div>'
            + "".join(f'<div style="font-size:14px;margin:4px 0">{html.escape(line)}</div>' for line in lines)
            + "</div>"
        )
        manager = self._ensure_push_manager()
        if not manager.has_channels:
            self.logger.warning("每周复盘无可用推送通道，保留待下次重试。")
            return
        allowed, reason = self._push_gate(stamp)
        if not allowed:
            self.logger.info("每周复盘暂不推送（%s），保留待下次重试。", reason)
            return
        outcomes = manager.send_card(
            "每周通知复盘",
            body,
            dedupe_key=f"weekly:{stamp:%G-%V}",
            summary="本周通知复盘",
            html=html_body,
        )
        if not any(item.ok for item in outcomes):
            self.logger.warning("每周复盘投递失败，保留待下次重试。")
            return
        if any(item.ok and not item.skipped for item in outcomes):
            self._note_push_sent(stamp)
        self.store.meta_set(key, iso(stamp))
        self.logger.info("已生成每周复盘。")

    def tick(self, now: dt.datetime | None = None) -> list[Digest]:
        stamp = now or now_local()
        self.last_tick_at = stamp
        self._maybe_catchup(stamp)
        self._maybe_deadline_reminders(stamp)
        self._maybe_weekly_review(stamp)
        self._maybe_candidate_reminders(stamp)
        produced: list[Digest] = []
        self._maybe_prune(stamp)

        records = self.store.unprocessed_messages(limit=self.settings.max_batch)
        if not records:
            self._retry()
            return produced

        analyses = analyse_records(records)
        history = self.store.recent_item_texts(hours=self.settings.dedupe_hours)
        urgent = urgent_analyses(analyses, self.settings, now=stamp)
        if urgent:
            urgent_ids = {item["msg_id"] for item in urgent}
            subset = [record for record in records if record["msg_id"] in urgent_ids]
            digest = build_digest(
                self.settings,
                subset,
                kind="urgent",
                now=stamp,
                history=history,
                event_splits=self._event_splits(),
            )
            self._flush_decisions(digest, delivered=self._publish(digest, when=stamp))
            produced.append(digest)
            records = [record for record in records if record["msg_id"] not in urgent_ids]
            if not records:
                self._retry()
                return produced

        immediate_records = [
            record
            for record in records
            if self.settings.is_immediate_group(str(record.get("group_id") or ""))
        ]
        if immediate_records:
            allowed, reason = self._push_gate(stamp)
            if allowed:
                immediate_ids = {record["msg_id"] for record in immediate_records}
                digest = build_digest(
                    self.settings,
                    immediate_records,
                    kind="window",
                    now=stamp,
                    history=history,
                    event_splits=self._event_splits(),
                )
                self._flush_decisions(digest, delivered=self._publish(digest, when=stamp))
                produced.append(digest)
                records = [record for record in records if record["msg_id"] not in immediate_ids]
                if not records:
                    self._retry()
                    return produced
            else:
                self._hold_batch(stamp, len(immediate_records), reason)
                self._record_deferred(
                    analyses,
                    reason,
                    msg_ids={record["msg_id"] for record in immediate_records},
                    when=stamp,
                )

        oldest = parse_iso(records[0].get("received_at")) or stamp
        elapsed = (stamp - oldest).total_seconds()
        window_seconds = self.settings.window_minutes * 60
        force = len(records) >= self.settings.max_batch or elapsed >= window_seconds * FORCE_FLUSH_MULTIPLIER
        if elapsed < window_seconds and not force:
            return produced

        allowed, reason = self._push_gate(stamp)
        if not allowed:
            self._hold_batch(stamp, len(records), reason)
            self._record_deferred(
                analyses, reason, msg_ids={record["msg_id"] for record in records}, when=stamp
            )
            self._retry()
            return produced

        digest = build_digest(
            self.settings,
            records,
            kind="window",
            now=stamp,
            history=history,
            event_splits=self._event_splits(),
        )
        if digest.items:
            self._flush_decisions(digest, delivered=self._publish(digest, when=stamp))
        else:
            digest.kind = "silent"
            digest.id = self.store.insert_digest(
                kind="silent",
                window_start=iso(digest.window_start),
                window_end=iso(digest.window_end),
                body="",
                payload=[],
                message_count=digest.message_count,
                llm_used=False,
            )
            self.store.mark_processed(digest.message_ids, digest.id)
            self._flush_decisions(digest, delivered=False)
            self.logger.info("窗口内 %d 条消息无重点，静默归档（未推送）。", digest.message_count)
        produced.append(digest)
        self._retry()
        return produced

    def _publish(
        self,
        digest: Digest,
        *,
        push_dedupe_key: str = "",
        when: dt.datetime | None = None,
    ) -> bool:
        stamp = when or now_local()
        if not digest.items:
            digest.id = self.store.insert_digest(
                kind="silent",
                window_start=iso(digest.window_start),
                window_end=iso(digest.window_end),
                body="",
                payload=[],
                message_count=digest.message_count,
                llm_used=False,
            )
            self.store.mark_processed(digest.message_ids, digest.id)
            self.logger.info(
                "本次 %d 条消息无可推送内容（重复或无需动手），静默归档未推送。",
                digest.message_count,
            )
            return False
        if digest.llm_error:
            self.last_llm_error = str(digest.llm_error)[:400]
            self.last_llm_error_at = stamp
            self._bump_meta_counter("llm_failures_total")
            # 只有可重试的失败才值得推迟；鉴权/参数错误直接降级，避免卡住推送。
            decision = (
                self._llm_defer_decision(stamp)
                if digest.llm_retryable
                else "fallback"
            )
            if decision == "defer":
                self._bump_meta_counter("llm_deferred_total")
                self._hold_batch(
                    stamp,
                    digest.message_count,
                    f"大模型失败，稍后重试：{digest.llm_error}",
                )
                self._record_deferred(
                    [
                        row
                        for row in (digest.decisions or [])
                        if str(row.get("outcome") or "") == decisions.PENDING
                    ],
                    "大模型失败，稍后重试",
                    when=stamp,
                )
                return False
            self._bump_meta_counter("llm_fallbacks_total")
            # 本批已用本地规则交付，推迟计数归零，避免影响后续批次。
            self._llm_defer_reset()
            if digest.llm_retryable:
                self.logger.error(
                    "大模型重试耗尽，本批 %d 条消息回退本地规则推送：%s",
                    digest.message_count,
                    digest.llm_error,
                )
            else:
                self.logger.warning(
                    "大模型不可用（%s），本批 %d 条消息使用本地规则推送。",
                    digest.llm_error,
                    digest.message_count,
                )
        else:
            self._llm_defer_reset()
        digest.id = self.store.insert_digest(
            kind=digest.kind,
            window_start=iso(digest.window_start),
            window_end=iso(digest.window_end),
            body=digest.body,
            payload=digest.payload(),
            message_count=digest.message_count,
            llm_used=digest.llm_used,
        )
        self.store.mark_processed(digest.message_ids, digest.id)
        manager = self._ensure_push_manager()
        tasks = self._sync_tasks(digest)
        if self.settings.html_push:
            digest.html = html_push_text(digest, self.settings)
        if not manager.has_channels:
            self.logger.error("没有可用推送通道，摘要仅归档到数据库（digest_id=%s）。", digest.id)
            return False
        outcomes = manager.send_digest(digest, dedupe_key=push_dedupe_key)
        delivered = [item for item in outcomes if item.ok and not item.skipped]
        failed = [item for item in outcomes if not item.ok]
        if delivered:
            self._note_push_sent(stamp)
        self.logger.info(
            "摘要 #%s（%s）投递：成功 %d，失败 %d，静默跳过 %d，新增任务 %d",
            digest.id,
            digest.kind,
            len(delivered),
            len(failed),
            len([item for item in outcomes if item.skipped]),
            len(tasks),
        )
        for item in failed:
            self.logger.warning("待重试 %s:%s -> %s", item.channel, item.target, item.error)
        return any(item.ok for item in outcomes)

    def _sync_tasks(self, digest: Digest) -> list[dict[str, Any]]:
        """把摘要里的行动项写入待办库；提醒类不重复建任务。"""
        if digest.kind in {"morning", "evening"}:
            return []
        changed: list[dict[str, Any]] = []
        for item in digest.items:
            if not is_task_eligible(item, self.settings):
                continue
            category = str(item.get("category") or "info")
            action = str(item.get("action") or "")
            deadline = item.get("deadline_dt")
            if category not in {"urgent", "action"} and not action and not isinstance(deadline, dt.datetime):
                continue  # 纯信息不进待办
            message = item["message"]
            summary = str(item.get("summary") or message.text[:60])
            if "链路自测" in summary or "链路自测" in message.text:
                continue  # 自测消息不进待办
            key = str(item.get("msg_id") or "")
            if not key:
                raw = f"{summary}|{deadline}|{message.text[:200]}"
                key = "auto:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
            groups = [str(name) for name in (item.get("duplicate_groups") or []) if str(name).strip()]
            if not groups and item.get("group_name"):
                groups = [str(item["group_name"])]
            classification = classify_task_item(item, self.settings)
            source = str(classification.get("source") or "qq_message")
            record = item.get("record") if isinstance(item.get("record"), dict) else {}
            event = str(record.get("event") or "")
            if "file" in event:
                source = "file"
            elif "image" in event:
                source = "image"
            task_id = self.store.upsert_task(
                task_key=key,
                summary=summary,
                audience=str(item.get("audience") or ""),
                condition=str(item.get("condition") or ""),
                details=[str(value) for value in (item.get("details") or []) if str(value).strip()],
                action=action,
                category=category,
                importance=int(item.get("importance") or 3),
                deadline=iso(deadline) if isinstance(deadline, dt.datetime) else "",
                groups=groups,
                sender=str(message.sender or ""),
                evidence=str(item.get("evidence") or ""),
                digest_id=int(digest.id or 0),
                status=str(classification.get("status") or "open"),
                confidence=float(classification.get("confidence") or 0.0),
                source=source,
                classification_reason=str(classification.get("reason") or ""),
                classification_detail={
                    "level": str(classification.get("level") or ""),
                    "triggers": [str(value) for value in (classification.get("triggers") or ())],
                },
            )
            if task_id:
                item["task_id"] = task_id
                item["task_url"] = self._task_url(task_id)
                changed.append({"id": task_id, **classification})
        if changed:
            candidates = sum(1 for item in changed if item.get("status") == "candidate")
            self.logger.info(
                "待办库更新：%d 条（候选 %d，摘要 #%s）。", len(changed), candidates, digest.id
            )
        return changed

    def import_existing_tasks(self, *, days: int = 7) -> int:
        """把历史摘要里的行动项回填到待办库，重复执行安全。"""
        cutoff = now_local() - dt.timedelta(days=max(1, int(days)))
        today_start = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
        total = 0
        for row in self.store.recent_digests(limit=400):
            kind = str(row.get("kind") or "")
            if kind in {"morning", "evening", "silent"}:
                continue
            created = parse_iso(row.get("created_at"))
            if created and created < cutoff:
                continue
            items = [payload_to_item(entry) for entry in (row.get("items") or []) if isinstance(entry, dict)]
            filtered: list[dict[str, Any]] = []
            for item in items:
                if not is_task_eligible(item, self.settings):
                    continue
                deadline = item.get("deadline_dt")
                if isinstance(deadline, dt.datetime) and deadline < today_start:
                    continue  # 已经过期的历史事项不再回填
                summary = str(item.get("summary") or "")
                text = str(item["message"].text or "")
                if "链路自测" in text or "链路自测" in summary:
                    continue
                if text.rstrip().endswith(("?", "？")) or summary.rstrip().endswith(("?", "？")):
                    continue  # 疑问句不是待办
                if not isinstance(deadline, dt.datetime) and str(item.get("category")) not in {"urgent", "action"}:
                    continue
                filtered.append(item)
            if not filtered:
                continue
            digest = Digest(
                kind=kind,
                window_start=parse_iso(row.get("window_start")),
                window_end=parse_iso(row.get("window_end")),
                items=filtered,
                message_count=int(row.get("message_count") or len(items)),
                id=int(row.get("id") or 0),
            )
            total += len(self._sync_tasks(digest))
        return total

    def _web_meta(self) -> dict[str, Any]:
        manager = self._push_manager
        return {
            "groups": [self.settings.group_name(gid) for gid in self.settings.group_whitelist],
            "channels": [pusher.name for pusher in (manager.pushers if manager else [])],
            "reminders": (
                f"{self.settings.deadline_morning} / {self.settings.deadline_evening}"
                if self.settings.deadline_reminders_enabled
                else ""
            ),
            "started_at": iso(self.started_at) if self.started_at else "",
            "insights": self.store.correction_insights(days=30, min_group_hits=3),
            "push_budget": (
                f"{int(self.settings.push_daily_budget)} 条/天"
                if int(self.settings.push_daily_budget) > 0
                else "不限"
            ),
            "quiet_hours": self.settings.quiet_hours or "未设置",
        }

    def _retry(self) -> None:
        manager = self._ensure_push_manager()
        if not manager.has_channels:
            return
        outcomes = manager.retry_pending()
        for item in outcomes:
            if item.ok:
                self.logger.info("重试成功 %s:%s（第 %d 次）", item.channel, item.target, item.attempts)
            else:
                self.logger.warning("重试失败 %s:%s -> %s", item.channel, item.target, item.error)

    def _maybe_prune(self, now: dt.datetime) -> None:
        last = parse_iso(self.store.meta_get("last_prune", ""))
        if last and (now - last).total_seconds() < PRUNE_INTERVAL_SECONDS:
            return
        removed = self.store.prune()
        self.store.meta_set("last_prune", iso(now))
        files = cleanup_files(
            self.settings.data_dir / "files",
            days=self.settings.attachment_retention_days,
        )
        if files:
            self.logger.info("清理 %d 个过期附件文件。", files)
        if removed:
            self.logger.info("清理 %d 条过期消息。", removed)

    # ------------------------------------------------------------------ 状态
    def health_payload(self) -> dict[str, Any]:
        return {
            "started_at": iso(self.started_at) if self.started_at else "",
            "last_tick_at": iso(self.last_tick_at) if self.last_tick_at else "",
            "catchup_enabled": bool(self.settings.catchup_enabled),
            "attachments": self._attachment_worker.describe() if self._attachment_worker else {},
            "tasks": self.store.task_stats(),
            "web": self._web.describe() if self._web else {"enabled": False},
            "last_catchup_at": iso(self.last_catchup_at) if self.last_catchup_at else "",
            "last_catchup_attempt_at": iso(self.last_catchup_attempt_at) if self.last_catchup_attempt_at else "",
            "llm": self._llm_health(),
            "push": self._push_health(),
            "counts": self.store.counts(),
        }

    def _llm_health(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.settings.llm_enabled and self.settings.dashscope_api_key),
            "model": self.settings.dashscope_model,
            "failures_total": self._meta_int("llm_failures_total"),
            "deferred_total": self._meta_int("llm_deferred_total"),
            "fallbacks_total": self._meta_int("llm_fallbacks_total"),
            "last_error": self.last_llm_error,
            "last_error_at": iso(self.last_llm_error_at) if self.last_llm_error_at else "",
        }

    def _push_health(self) -> dict[str, Any]:
        stamp = now_local()
        names = sorted({pusher.name for pusher in (self._pushers or [])})
        if not names:
            # 还没推送过时退回配置视角，避免 /health 显示成“没有通道”。
            names = sorted(self.settings.push_channels())
        return {
            "channels": names,
            "sent_today": self._push_sent_today(stamp),
            "daily_budget": int(self.settings.push_daily_budget or 0),
            "quiet_hours": self.settings.quiet_hours,
            "last_push_at": iso(self.last_push_at) if self.last_push_at else "",
            "last_defer_reason": self.last_defer_reason,
            "last_defer_at": iso(self.last_defer_at) if self.last_defer_at else "",
            "deliveries": self.store.delivery_stats(),
        }

    def status(self) -> dict[str, Any]:
        self._ensure_push_manager()
        manager = self._push_manager
        return {
            "started_at": iso(self.started_at) if self.started_at else "",
            "last_tick_at": iso(self.last_tick_at) if self.last_tick_at else "",
            "catchup_enabled": self.settings.catchup_enabled,
            "last_catchup_at": iso(self.last_catchup_at) if self.last_catchup_at else "",
            "last_catchup_attempt_at": iso(self.last_catchup_attempt_at) if self.last_catchup_attempt_at else "",
            "bot_alive": bool(getattr(self._bot, "is_alive", False)),
            "bot_ready": bool(getattr(self._bot, "is_ready", False)),
            "bot_error": str(getattr(self._bot, "last_error", "") or ""),
            "receiver_alive": bool(self._receiver and self._receiver.is_alive),
            "channels": [pusher.describe() for pusher in (manager.pushers if manager else [])],
            "counts": self.store.counts(),
            "tasks": self.store.task_stats(),
            "llm_usage": self.store.llm_call_summary(hours=24),
        }
CATCHUP_RETRY_SECONDS = 5 * 60
DEADLINE_LOOKBACK_HOURS = 72
DEADLINE_MAX_ITEMS = 5
