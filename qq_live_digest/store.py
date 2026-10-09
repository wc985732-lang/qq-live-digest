"""SQLite 持久化：消息去重、摘要归档、投递去重与重启恢复。"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import datetime as dt
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from . import decisions
from . import llmstats
from .timeutil import iso, now_local, parse_iso

LOGGER = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    msg_id       TEXT PRIMARY KEY,
    source       TEXT NOT NULL DEFAULT 'qqbot',
    event        TEXT NOT NULL DEFAULT '',
    group_id     TEXT NOT NULL,
    group_name   TEXT NOT NULL DEFAULT '',
    sender_id    TEXT NOT NULL DEFAULT '',
    sender_name  TEXT NOT NULL DEFAULT '',
    ts           TEXT NOT NULL DEFAULT '',
    received_at  TEXT NOT NULL,
    content      TEXT NOT NULL DEFAULT '',
    source_text  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messages_received ON messages(received_at);
CREATE INDEX IF NOT EXISTS idx_messages_group ON messages(group_id, received_at);

CREATE TABLE IF NOT EXISTS processed (
    msg_id       TEXT PRIMARY KEY REFERENCES messages(msg_id),
    digest_id    INTEGER,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,
    window_start  TEXT NOT NULL DEFAULT '',
    window_end    TEXT NOT NULL DEFAULT '',
    item_count    INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    llm_used      INTEGER NOT NULL DEFAULT 0,
    body          TEXT NOT NULL DEFAULT '',
    payload       TEXT NOT NULL DEFAULT '[]',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    digest_id   INTEGER NOT NULL,
    channel     TEXT NOT NULL,
    target      TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(dedupe_key, channel, target)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status, updated_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_key    TEXT NOT NULL UNIQUE,
    summary     TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT 'info',
    importance  INTEGER NOT NULL DEFAULT 3,
    deadline    TEXT NOT NULL DEFAULT '',
    groups      TEXT NOT NULL DEFAULT '[]',
    sender      TEXT NOT NULL DEFAULT '',
    evidence    TEXT NOT NULL DEFAULT '',
    audience      TEXT NOT NULL DEFAULT '',
    condition_text TEXT NOT NULL DEFAULT '',
    details       TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL DEFAULT 'open',
    digest_id   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    done_at     TEXT NOT NULL DEFAULT ''
    ,snooze_until TEXT NOT NULL DEFAULT ''
    ,duplicate_of INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, deadline, importance);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);

CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL,
    event       TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_task_events_created ON task_events(created_at);

CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id        TEXT NOT NULL DEFAULT '',
    group_id      TEXT NOT NULL DEFAULT '',
    digest_id     INTEGER NOT NULL DEFAULT 0,
    stage         TEXT NOT NULL DEFAULT '',
    outcome       TEXT NOT NULL DEFAULT '',
    reason        TEXT NOT NULL DEFAULT '',
    score         INTEGER NOT NULL DEFAULT 0,
    min_score     INTEGER NOT NULL DEFAULT 0,
    category      TEXT NOT NULL DEFAULT '',
    rule_hits     TEXT NOT NULL DEFAULT '[]',
    dedupe_reason TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_msg ON decisions(msg_id, id);
CREATE INDEX IF NOT EXISTS idx_decisions_created ON decisions(created_at);
CREATE INDEX IF NOT EXISTS idx_decisions_outcome ON decisions(outcome, created_at);

CREATE TABLE IF NOT EXISTS event_splits (
    key        TEXT PRIMARY KEY,
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT NOT NULL,
    purpose           TEXT NOT NULL DEFAULT '',
    provider          TEXT NOT NULL DEFAULT '',
    model             TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'ok',
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    attempts          INTEGER NOT NULL DEFAULT 1,
    retried           INTEGER NOT NULL DEFAULT 0,
    fallback          INTEGER NOT NULL DEFAULT 0,
    error             TEXT NOT NULL DEFAULT '',
    route             TEXT NOT NULL DEFAULT '',
    route_reason      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls(created_at);
CREATE INDEX IF NOT EXISTS idx_llm_calls_status ON llm_calls(status, created_at);
"""


TASK_STATUSES = {"candidate", "open", "done", "dismissed", "expired"}


#: 人工纠错类型 → 人话标签（A8 反馈回收展示用）。
CORRECTION_LABELS = {
    "not_notice": "误判为通知",
    "not_task": "误判为待办",
    "not_urgent": "误判紧急",
    "duplicate": "重复",
    "category": "改了分类",
    "deadline": "改了截止时间",
    "clear_deadline": "清空截止时间",
}


MESSAGE_COLUMN_MIGRATIONS = {"source_text": "TEXT NOT NULL DEFAULT ''"}

TASK_COLUMN_MIGRATIONS = {
    "audience": "TEXT NOT NULL DEFAULT ''",
    "condition_text": "TEXT NOT NULL DEFAULT ''",
    "details": "TEXT NOT NULL DEFAULT '[]'",
    "confidence": "REAL NOT NULL DEFAULT 0",
    "source": "TEXT NOT NULL DEFAULT ''",
    "confirmed_at": "TEXT NOT NULL DEFAULT ''",
    "dismissed_at": "TEXT NOT NULL DEFAULT ''",
    "last_reminded_at": "TEXT NOT NULL DEFAULT ''",
    "remind_count": "INTEGER NOT NULL DEFAULT 0",
    "snooze_until": "TEXT NOT NULL DEFAULT ''",
    "duplicate_of": "INTEGER NOT NULL DEFAULT 0",
}

# A6 分级路由：老库补两列，缺省空串（渲染时算「未分级」）。
LLM_CALL_COLUMN_MIGRATIONS = {
    "route": "TEXT NOT NULL DEFAULT ''",
    "route_reason": "TEXT NOT NULL DEFAULT ''",
}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Store:
    def __init__(self, path: Path | str, *, retention_days: int = 30) -> None:
        self.path = Path(path)
        self.retention_days = retention_days
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.path.parent / "messages.jsonl"
        self.digest_jsonl_path = self.path.parent / "digests.jsonl"
        self.init()

    # ---------------------------------------------------------------- 基础设施
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def init(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate_messages(connection)
            self._migrate_tasks(connection)
            self._migrate_llm_calls(connection)

    @staticmethod
    def _migrate_messages(connection: sqlite3.Connection) -> None:
        existing = {str(row["name"]) for row in connection.execute("PRAGMA table_info(messages)").fetchall()}
        for name, definition in MESSAGE_COLUMN_MIGRATIONS.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")

    @staticmethod
    def _migrate_tasks(connection: sqlite3.Connection) -> None:
        existing = {str(row["name"]) for row in connection.execute("PRAGMA table_info(tasks)").fetchall()}
        for name, definition in TASK_COLUMN_MIGRATIONS.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")

    @staticmethod
    def _migrate_llm_calls(connection: sqlite3.Connection) -> None:
        existing = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(llm_calls)").fetchall()
        }
        for name, definition in LLM_CALL_COLUMN_MIGRATIONS.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE llm_calls ADD COLUMN {name} {definition}")

    @staticmethod
    def _task_event(
        connection: sqlite3.Connection,
        task_id: int,
        event: str,
        detail: Any = "",
        *,
        created_at: Any = None,
    ) -> None:
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail, ensure_ascii=False, default=str)
        connection.execute(
            "INSERT INTO task_events (task_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
            (int(task_id), str(event), str(detail or ""), iso(created_at or now_local())),
        )

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except OSError as error:  # 审计日志失败不应影响主流程
            LOGGER.warning("写入 JSONL 失败 %s: %s", path, error)

    # ------------------------------------------------------------------- 消息
    def insert_message(
        self,
        *,
        msg_id: str,
        group_id: str,
        content: str,
        source_text: str = "",
        ts: Any = None,
        received_at: Any = None,
        source: str = "qqbot",
        event: str = "",
        sender_id: str = "",
        sender_name: str = "",
        group_name: str = "",
    ) -> bool:
        """写入消息，msg_id 重复时返回 False。"""
        msg_id = str(msg_id or "").strip()
        if not msg_id:
            raise ValueError("msg_id 不能为空")
        received = iso(received_at or now_local())
        stamp = iso(ts) or received
        payload = {
            "msg_id": msg_id,
            "source": source,
            "event": event,
            "group_id": str(group_id or ""),
            "group_name": group_name,
            "sender_id": str(sender_id or ""),
            "sender_name": sender_name,
            "ts": stamp,
            "received_at": received,
            "content": content or "",
            "source_text": source_text or "",
        }
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO messages
                    (msg_id, source, event, group_id, group_name, sender_id, sender_name, ts, received_at, content, source_text)
                VALUES (:msg_id, :source, :event, :group_id, :group_name, :sender_id, :sender_name, :ts, :received_at, :content, :source_text)
                """,
                payload,
            )
            inserted = cursor.rowcount > 0
        if inserted:
            self._append_jsonl(self.jsonl_path, payload)
        return inserted

    def search_messages(
        self, query: str, *, limit: int = 20, hours: int = 0, group_id: str = ""
    ) -> list[dict[str, Any]]:
        """按正文子串搜历史消息（只读，Roadmap A1）；hours>0 时只看最近 N 小时。"""
        text = str(query or "").strip()
        if not text:
            return []
        like = f"%{text}%"
        where = ["(content LIKE ? OR source_text LIKE ?)"]
        params: list[Any] = [like, like]
        if group_id:
            where.append("group_id = ?")
            params.append(str(group_id))
        if int(hours or 0) > 0:
            where.append("received_at >= ?")
            params.append(iso(now_local() - dt.timedelta(hours=int(hours))))
        params.append(max(1, min(200, int(limit or 20))))
        sql = (
            "SELECT msg_id, group_id, group_name, sender_name, ts, received_at, content, source_text"
            " FROM messages WHERE "
            + " AND ".join(where)
            + " ORDER BY received_at DESC, msg_id DESC LIMIT ?"
        )
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def unprocessed_messages(self, *, limit: int = 400) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.* FROM messages m
                LEFT JOIN processed p ON p.msg_id = m.msg_id
                WHERE p.msg_id IS NULL
                ORDER BY m.received_at ASC, m.msg_id ASC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_processed(self, msg_ids: Iterable[str], digest_id: int | None) -> int:
        ids = [str(item) for item in msg_ids if str(item or "").strip()]
        if not ids:
            return 0
        stamp = iso(now_local())
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO processed (msg_id, digest_id, processed_at) VALUES (?, ?, ?)",
                [(msg_id, digest_id, stamp) for msg_id in ids],
            )
        return len(ids)

    # --------------------------------------------------------------- 决策日志
    def record_decisions(self, rows: Iterable[dict[str, Any]]) -> int:
        """批量写入决策轨迹。

        字段缺失一律按默认值处理：决策日志是**旁路观测**，多一个键或少一个键都不该影响主链路。
        """
        stamp = iso(now_local())
        prepared: list[tuple[Any, ...]] = []
        superseded: dict[str, None] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            outcome = str(row.get("outcome") or "")
            msg_id = str(row.get("msg_id") or "")
            if not outcome and not msg_id:
                continue
            stage = str(row.get("stage") or "")
            if decisions.is_final(outcome) and msg_id:
                superseded[msg_id] = None
            hits = row.get("rule_hits") or []
            if not isinstance(hits, str):
                hits = json.dumps([str(item) for item in hits], ensure_ascii=False)
            prepared.append(
                (
                    msg_id,
                    str(row.get("group_id") or ""),
                    _as_int(row.get("digest_id")),
                    stage,
                    outcome,
                    str(row.get("reason") or ""),
                    _as_int(row.get("score")),
                    _as_int(row.get("min_score")),
                    str(row.get("category") or ""),
                    hits,
                    str(row.get("dedupe_reason") or ""),
                    iso(row.get("created_at") or stamp),
                )
            )
        if not prepared:
            return 0
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO decisions
                    (msg_id, group_id, digest_id, stage, outcome, reason, score, min_score,
                     category, rule_hits, dedupe_reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                prepared,
            )
            if superseded:
                # 一条消息一旦有了最终结论，它之前的「延后未决」过程行就该消失，
                # 否则同一条消息会既显示"延后"又显示"已推送"，查询时自相矛盾。
                connection.executemany(
                    "DELETE FROM decisions WHERE outcome = ? AND msg_id = ?",
                    [(decisions.DEFERRED, msg_id) for msg_id in superseded],
                )
        return len(prepared)

    def decisions_for(self, msg_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """一条消息的完整决策轨迹（新→旧）。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM decisions WHERE msg_id = ? ORDER BY id DESC LIMIT ?",
                (str(msg_id or ""), max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_decisions(self, *, limit: int = 30, outcome: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM decisions"
        params: list[Any] = []
        if str(outcome or "").strip():
            sql += " WHERE outcome = ?"
            params.append(str(outcome).strip())
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def decision_counts(
        self, *, hours: int = 24, now: dt.datetime | None = None
    ) -> dict[str, int]:
        """按时间窗统计各结论条数；now 可注入，便于测试锁定参考时刻。"""
        cutoff = iso((now or now_local()) - dt.timedelta(hours=max(1, int(hours))))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT outcome, COUNT(*) AS total FROM decisions WHERE created_at >= ? GROUP BY outcome",
                (cutoff,),
            ).fetchall()
        return {str(row["outcome"] or "unknown"): int(row["total"] or 0) for row in rows}

    # --------------------------------------------------------------- 模型用量
    def add_llm_call(self, call: Any, *, created_at: Any = None) -> int:
        """写入一条模型用量记录（Roadmap A5）。

        `call` 是 `providers.LLMCall`（按字段鸭子类型读取，store 不反向依赖 providers）。
        一次逻辑调用只落一行，重试次数记在 `attempts`，失败原因记在 `error`。
        """
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO llm_calls (
                    created_at, purpose, provider, model, status,
                    prompt_tokens, completion_tokens, latency_ms,
                    attempts, retried, fallback, error, route, route_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    iso(created_at or now_local()),
                    str(getattr(call, "purpose", "") or ""),
                    str(getattr(call, "provider", "") or ""),
                    str(getattr(call, "model", "") or ""),
                    str(getattr(call, "status", "") or llmstats.STATUS_OK),
                    max(0, _as_int(getattr(call, "prompt_tokens", 0))),
                    max(0, _as_int(getattr(call, "completion_tokens", 0))),
                    max(0, _as_int(getattr(call, "latency_ms", 0))),
                    max(0, _as_int(getattr(call, "attempts", 1), 1)),
                    int(bool(getattr(call, "retried", False))),
                    int(bool(getattr(call, "fallback", False))),
                    str(getattr(call, "error", "") or "")[:500],
                    str(getattr(call, "route", "") or ""),
                    str(getattr(call, "route_reason", "") or "")[:200],
                ),
            )
            return int(cursor.lastrowid or 0)

    def llm_calls_between(self, start: Any, end: Any = None, *, limit: int = 0) -> list[dict[str, Any]]:
        """按时间区间取用量明细（新→旧）；`end` 为空表示到最新。"""
        sql = "SELECT * FROM llm_calls WHERE created_at >= ?"
        params: list[Any] = [iso(start)]
        if end is not None:
            sql += " AND created_at < ?"
            params.append(iso(end))
        sql += " ORDER BY created_at DESC, id DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def llm_calls_since(self, start: Any, *, limit: int = 0) -> list[dict[str, Any]]:
        return self.llm_calls_between(start, None, limit=limit)

    def recent_llm_calls(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM llm_calls ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
            ).fetchall()
        return [dict(row) for row in rows]

    def llm_call_summary(self, *, hours: int = 24) -> dict[str, int]:
        """最近 N 小时的用量小结（调用数 / 失败 / token / 耗时），供 doctor 一眼看。"""
        window = max(1, int(hours))
        cutoff = iso(now_local() - dt.timedelta(hours=window))
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS calls,
                       SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS skipped,
                       COALESCE(SUM(retried), 0) AS retried,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(latency_ms), 0) AS latency_ms
                FROM llm_calls WHERE created_at >= ?
                """,
                (llmstats.STATUS_ERROR, llmstats.STATUS_SKIPPED, cutoff),
            ).fetchone()
        summary = {
            key: int(row[key] or 0)
            for key in ("calls", "failed", "skipped", "retried", "prompt_tokens", "completion_tokens", "latency_ms")
        }
        summary["hours"] = window
        return summary

    def record_deferred(self, rows: Iterable[dict[str, Any]]) -> int:
        """写入 / 刷新「延后未决」过程行。

        同一条消息在每个 tick 都可能被推迟一次（夜间静默会持续好几个小时），所以这里按
        `msg_id` **就地更新**为最新原因，而不是每次插一行——否则一晚就能把表撑爆。
        消息一旦拿到最终结论，`record_decisions` 会把这些过程行清掉。
        """
        stamp = iso(now_local())
        prepared: list[tuple[Any, ...]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            msg_id = str(row.get("msg_id") or "")
            if not msg_id:
                continue
            hits = row.get("rule_hits") or []
            if not isinstance(hits, str):
                hits = json.dumps([str(item) for item in hits], ensure_ascii=False)
            prepared.append(
                (
                    msg_id,
                    str(row.get("group_id") or ""),
                    _as_int(row.get("digest_id")),
                    str(row.get("stage") or decisions.STAGE_PUBLISH),
                    str(row.get("reason") or ""),
                    _as_int(row.get("score")),
                    _as_int(row.get("min_score")),
                    str(row.get("category") or ""),
                    hits,
                    iso(row.get("created_at") or stamp),
                )
            )
        if not prepared:
            return 0
        touched = 0
        with self._connect() as connection:
            for (
                msg_id,
                group_id,
                digest_id,
                stage,
                reason,
                score,
                min_score,
                category,
                hits,
                created_at,
            ) in prepared:
                cursor = connection.execute(
                    """
                    UPDATE decisions
                       SET group_id = ?, digest_id = ?, stage = ?, reason = ?, score = ?,
                           min_score = ?, category = ?, rule_hits = ?, dedupe_reason = '',
                           created_at = ?
                     WHERE msg_id = ? AND outcome = ?
                    """,
                    (
                        group_id,
                        digest_id,
                        stage,
                        reason,
                        score,
                        min_score,
                        category,
                        hits,
                        created_at,
                        msg_id,
                        decisions.DEFERRED,
                    ),
                )
                if cursor.rowcount:
                    touched += 1
                    continue
                connection.execute(
                    """
                    INSERT INTO decisions
                        (msg_id, group_id, digest_id, stage, outcome, reason, score, min_score,
                         category, rule_hits, dedupe_reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)
                    """,
                    (
                        msg_id,
                        group_id,
                        digest_id,
                        stage,
                        decisions.DEFERRED,
                        reason,
                        score,
                        min_score,
                        category,
                        hits,
                        created_at,
                    ),
                )
                touched += 1
        return touched

    def deferred_decisions(
        self, *, limit: int = 30, hours: int = 24, now: dt.datetime | None = None
    ) -> list[dict[str, Any]]:
        """当前还「延后未决」的消息（新 → 旧）；now 可注入，便于测试锁定参考时刻。"""
        cutoff = iso((now or now_local()) - dt.timedelta(hours=max(1, int(hours))))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM decisions
                 WHERE outcome = ? AND created_at >= ?
                 ORDER BY created_at DESC, id DESC
                 LIMIT ?
                """,
                (decisions.DEFERRED, cutoff, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def deferred_count(self, *, hours: int = 24, now: dt.datetime | None = None) -> int:
        cutoff = iso((now or now_local()) - dt.timedelta(hours=max(1, int(hours))))
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM decisions WHERE outcome = ? AND created_at >= ?",
                (decisions.DEFERRED, cutoff),
            ).fetchone()
        return int(row[0] or 0) if row else 0

    # --------------------------------------------------- 事件级跨群聚合（A11）
    def add_event_split(
        self, key: str, *, reason: str = "", created_at: Any = None
    ) -> bool:
        """记一条「这个事件不要自动合并」的拆分覆盖（幂等）。"""
        value = str(key or "").strip()
        if not value:
            return False
        stamp = (
            iso(created_at)
            if isinstance(created_at, dt.datetime)
            else str(created_at or iso(now_local()))
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO event_splits(key, reason, created_at) VALUES (?, ?, ?)",
                (value, str(reason or ""), stamp),
            )
        return True

    def remove_event_split(self, key: str) -> bool:
        value = str(key or "").strip()
        if not value:
            return False
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM event_splits WHERE key = ?", (value,))
        return cursor.rowcount > 0

    def event_splits(self) -> list[dict[str, Any]]:
        """所有拆分覆盖（新 → 旧）。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT key, reason, created_at FROM event_splits ORDER BY created_at DESC, key"
            ).fetchall()
        return [dict(row) for row in rows]

    def event_split_keys(self) -> tuple[str, ...]:
        return tuple(
            str(row["key"]) for row in self.event_splits() if str(row.get("key") or "").strip()
        )

    def oldest_unprocessed(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT MIN(m.received_at) AS oldest FROM messages m
                LEFT JOIN processed p ON p.msg_id = m.msg_id
                WHERE p.msg_id IS NULL
                """
            ).fetchone()
        return str(row["oldest"] or "") if row else ""

    # ------------------------------------------------------------------- 摘要
    def insert_digest(
        self,
        *,
        kind: str,
        window_start: str,
        window_end: str,
        body: str,
        payload: list[dict[str, Any]],
        message_count: int,
        llm_used: bool,
    ) -> int:
        record = {
            "kind": kind,
            "window_start": window_start,
            "window_end": window_end,
            "message_count": int(message_count),
            "item_count": len(payload),
            "llm_used": bool(llm_used),
            "body": body,
            "created_at": iso(now_local()),
        }
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO digests
                    (kind, window_start, window_end, item_count, message_count, llm_used, body, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["kind"],
                    record["window_start"],
                    record["window_end"],
                    record["item_count"],
                    record["message_count"],
                    int(record["llm_used"]),
                    record["body"],
                    json.dumps(payload, ensure_ascii=False, default=str),
                    record["created_at"],
                ),
            )
            digest_id = int(cursor.lastrowid or 0)
        record["id"] = digest_id
        self._append_jsonl(self.digest_jsonl_path, record)
        return digest_id

    def get_digest(self, digest_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM digests WHERE id = ?", (int(digest_id),)).fetchone()
        if not row:
            return None
        data = dict(row)
        try:
            data["items"] = json.loads(data.get("payload") or "[]")
        except json.JSONDecodeError:
            data["items"] = []
        return data

    # ------------------------------------------------------------------- 投递
    def claim_delivery(
        self,
        *,
        digest_id: int,
        channel: str,
        target: str,
        dedupe_key: str,
        max_attempts: int,
        retry_seconds: int,
    ) -> tuple[bool, int | None]:
        """返回 (是否应立即发送, delivery_id)。

        已成功发送的写入 sent，永不重发；失败的在冷却期后允许重试，直到达到上限。
        """
        stamp = now_local()
        stamp_text = iso(stamp)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, status, attempts, updated_at FROM deliveries WHERE dedupe_key=? AND channel=? AND target=?",
                (dedupe_key, channel, target),
            ).fetchone()
            if row is None:
                cursor = connection.execute(
                    """
                    INSERT INTO deliveries
                        (digest_id, channel, target, dedupe_key, status, attempts, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'pending', 1, ?, ?)
                    """,
                    (int(digest_id), channel, target, dedupe_key, stamp_text, stamp_text),
                )
                return True, int(cursor.lastrowid or 0)

            delivery_id = int(row["id"])
            if row["status"] == "sent":
                return False, delivery_id
            if int(row["attempts"]) >= int(max_attempts):
                return False, delivery_id
            updated = parse_iso(row["updated_at"])
            if updated and (stamp - updated).total_seconds() < int(retry_seconds):
                return False, delivery_id
            connection.execute(
                """
                UPDATE deliveries SET attempts = attempts + 1, status='pending', updated_at=?
                WHERE id=?
                """,
                (stamp_text, delivery_id),
            )
            return True, delivery_id

    def get_delivery(self, delivery_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, status, attempts, last_error, updated_at FROM deliveries WHERE id=?",
                (int(delivery_id),),
            ).fetchone()
        return dict(row) if row else None

    def mark_delivery(self, delivery_id: int, *, ok: bool, error: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE deliveries SET status=?, last_error=?, updated_at=? WHERE id=?",
                ("sent" if ok else "failed", (error or "")[:500], iso(now_local()), int(delivery_id)),
            )

    def increment_delivery_attempt(self, delivery_id: int) -> int:
        """重试已在 pending_deliveries 中筛过，这里只累加尝试次数。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempts FROM deliveries WHERE id=?", (int(delivery_id),)
            ).fetchone()
            attempts = int(row["attempts"]) + 1 if row else 0
            if row:
                connection.execute(
                    "UPDATE deliveries SET attempts=?, status='pending', updated_at=? WHERE id=?",
                    (attempts, iso(now_local()), int(delivery_id)),
                )
        return attempts

    def pending_deliveries(self, *, max_attempts: int, retry_seconds: int) -> list[dict[str, Any]]:
        threshold = now_local().timestamp() - int(retry_seconds)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.*, g.kind, g.window_start, g.window_end, g.body, g.item_count
                FROM deliveries d JOIN digests g ON g.id = d.digest_id
                WHERE d.status != 'sent' AND d.attempts < ?
                ORDER BY d.updated_at ASC
                LIMIT 50
                """,
                (int(max_attempts),),
            ).fetchall()
        pending: list[dict[str, Any]] = []
        for row in rows:
            updated = parse_iso(row["updated_at"])
            if updated and updated.timestamp() > threshold:
                continue
            pending.append(dict(row))
        return pending

    # ------------------------------------------------------------------- 元信息
    # ------------------------------------------------------------------- 去重
    def recent_items(self, *, hours: int = 6, limit: int = 200) -> list[dict[str, Any]]:
        """最近推送过的条目，用于跨群、跨窗口的重复通知抑制。"""
        if hours <= 0:
            return []
        cutoff = iso(now_local() - dt.timedelta(hours=hours))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, kind, payload, created_at FROM digests
                WHERE created_at >= ? AND item_count > 0
                ORDER BY id DESC LIMIT ?
                """,
                (cutoff, max(1, int(limit))),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, list):
                continue
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                item = dict(entry)
                item["digest_id"] = int(row["id"] or 0)
                item["kind"] = str(row["kind"] or "")
                item["created_at"] = str(row["created_at"] or "")
                items.append(item)
        return items

    def recent_item_texts(self, *, hours: int = 6, limit: int = 200) -> list[dict[str, Any]]:
        """最近推送过的条目文本，用于跨群、跨窗口的重复通知抑制。"""
        return [
            {
                "text": str(item.get("text") or item.get("summary") or ""),
                "summary": str(item.get("summary") or ""),
                "group": str(item.get("group") or ""),
                "ts": str(item.get("created_at") or ""),
            }
            for item in self.recent_items(hours=hours, limit=limit)
        ]

    def recent_digests(self, *, limit: int = 5) -> list[dict[str, Any]]:
        """最近几条摘要（含条目 payload），供 show 命令回溯判断依据。"""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, kind, window_start, window_end, item_count, message_count, body, payload, created_at
                FROM digests ORDER BY id DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            try:
                data["items"] = json.loads(data.get("payload") or "[]")
            except (json.JSONDecodeError, TypeError):
                data["items"] = []
            result.append(data)
        return result
    # ------------------------------------------------------------------- 任务
    def upsert_task(
        self,
        *,
        task_key: str,
        summary: str,
        audience: str = "",
        condition: str = "",
        details: Iterable[str] = (),
        action: str = "",
        category: str = "info",
        importance: int = 3,
        deadline: str = "",
        groups: Iterable[str] = (),
        sender: str = "",
        evidence: str = "",
        digest_id: int = 0,
        status: str = "open",
        confidence: float = 0.0,
        source: str = "",
        classification_reason: str = "",
        classification_detail: Mapping[str, Any] | None = None,
    ) -> int:
        """写入或更新任务；重复出现时保留用户处理过的状态。"""
        key = str(task_key or "").strip()
        if not key:
            return 0
        status = str(status or "open")
        if status not in TASK_STATUSES:
            status = "open"
        stamp = iso(now_local())
        group_list = [str(name) for name in groups if str(name or "").strip()]
        groups_json = json.dumps(list(dict.fromkeys(group_list)), ensure_ascii=False)
        detail_list = [str(value) for value in details if str(value or "").strip()]
        details_json = json.dumps(detail_list, ensure_ascii=False)
        extra = {
            str(key): value
            for key, value in dict(classification_detail or {}).items()
            if value not in (None, "", (), [], {})
        }
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT id, status FROM tasks WHERE task_key = ?", (key,)
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO tasks
                        (task_key, summary, action, audience, condition_text, details,
                         category, importance, deadline, groups, sender, evidence,
                         status, confidence, source, digest_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        str(summary or ""),
                        str(action or ""),
                        str(audience or ""),
                        str(condition or ""),
                        details_json,
                        str(category or "info"),
                        int(importance or 3),
                        str(deadline or ""),
                        groups_json,
                        str(sender or ""),
                        str(evidence or ""),
                        status,
                        float(confidence or 0.0),
                        str(source or ""),
                        int(digest_id or 0),
                        stamp,
                        stamp,
                    ),
                )
                task_id = int(cursor.lastrowid or 0)
                self._task_event(
                    connection,
                    task_id,
                    "created",
                    {
                        "status": status,
                        "confidence": float(confidence or 0.0),
                        "reason": classification_reason,
                        **extra,
                    },
                    created_at=stamp,
                )
                if status == "candidate":
                    self._task_event(
                        connection,
                        task_id,
                        "candidate_detected",
                        {
                            "confidence": float(confidence or 0.0),
                            "reason": classification_reason,
                            **extra,
                        },
                        created_at=stamp,
                    )
                return task_id

            task_id = int(existing["id"])
            connection.execute(
                """
                UPDATE tasks SET
                    summary=?,
                    action=?,
                    audience=?,
                    condition_text=?,
                    details=?,
                    category=?,
                    importance=MAX(importance, ?),
                    deadline=CASE WHEN ? <> '' THEN ? ELSE deadline END,
                    groups=?,
                    sender=?,
                    evidence=CASE WHEN ? <> '' THEN ? ELSE evidence END,
                    confidence=MAX(confidence, ?),
                    source=CASE WHEN ? <> '' THEN ? ELSE source END,
                    digest_id=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    str(summary or ""),
                    str(action or ""),
                    str(audience or ""),
                    str(condition or ""),
                    details_json,
                    str(category or "info"),
                    int(importance or 3),
                    str(deadline or ""),
                    str(deadline or ""),
                    groups_json,
                    str(sender or ""),
                    str(evidence or ""),
                    str(evidence or ""),
                    float(confidence or 0.0),
                    str(source or ""),
                    str(source or ""),
                    int(digest_id or 0),
                    stamp,
                    task_id,
                ),
            )
            return task_id

    def list_tasks(
        self,
        *,
        include_done: bool = True,
        statuses: Iterable[str] | None = None,
        limit: int = 800,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        status_list = [str(item) for item in statuses] if statuses is not None else None
        if status_list:
            where.append("t.status IN (" + ",".join("?" for _ in status_list) + ")")
            params.extend(status_list)
        elif not include_done:
            where.append("t.status = 'open'")
        sql = """
            SELECT t.*,
                   (SELECT detail FROM task_events e
                    WHERE e.task_id = t.id AND e.event = 'candidate_detected'
                    ORDER BY e.id DESC LIMIT 1) AS candidate_detail
                   ,(SELECT COALESCE(NULLIF(m.source_text, ''), m.content) FROM messages m
                     WHERE m.msg_id = t.task_key LIMIT 1) AS source_text
                   ,(SELECT d.summary FROM tasks d WHERE d.id = t.duplicate_of) AS duplicate_summary
            FROM tasks t
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += (
            " ORDER BY CASE WHEN t.deadline = '' THEN 1 ELSE 0 END, t.deadline ASC,"
            " t.importance DESC, t.id DESC LIMIT ?"
        )
        params.append(max(1, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        return [self._task_row(row) for row in rows]

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT t.*,
                       (SELECT detail FROM task_events e
                        WHERE e.task_id = t.id AND e.event = 'candidate_detected'
                        ORDER BY e.id DESC LIMIT 1) AS candidate_detail
                       ,(SELECT COALESCE(NULLIF(m.source_text, ''), m.content) FROM messages m
                         WHERE m.msg_id = t.task_key LIMIT 1) AS source_text
                       ,(SELECT d.summary FROM tasks d WHERE d.id = t.duplicate_of) AS duplicate_summary
                FROM tasks t WHERE t.id = ?
                """,
                (int(task_id),),
            ).fetchone()
        return self._task_row(row) if row else None

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict[str, Any]:
        task = dict(row)
        try:
            groups = json.loads(task.get("groups") or "[]")
        except (json.JSONDecodeError, TypeError):
            groups = []
        task["groups"] = [str(name) for name in groups] if isinstance(groups, list) else []
        try:
            details = json.loads(task.get("details") or "[]")
        except (json.JSONDecodeError, TypeError):
            details = []
        task["details"] = [str(value) for value in details] if isinstance(details, list) else []
        task["condition"] = str(task.pop("condition_text", "") or "")
        task["done"] = str(task.get("status") or "") == "done"
        task["snooze_until"] = str(task.get("snooze_until") or "")
        task["duplicate_of"] = int(task.get("duplicate_of") or 0)
        return task

    def list_open_tasks(self, *, limit: int = 800) -> list[dict[str, Any]]:
        return self.list_tasks(statuses=("open",), limit=limit)

    def list_task_events(self, task_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, task_id, event, detail, created_at
                FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT ?
                """,
                (int(task_id), max(1, int(limit)))
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _normalize_summary(text: str) -> str:
        return re.sub(r"[\s，。、；：！？,.!?;:（）()\[\]【】\"'“”‘’\-_—]+", "", str(text or "")).lower()

    @classmethod
    def _resolve_duplicate_target(
        cls,
        connection: sqlite3.Connection,
        *,
        task_id: int,
        summary: str,
        requested: Any,
    ) -> int:
        """找到“重复”指向的原始任务：优先用显式任务号，否则按摘要文本匹配。"""
        try:
            candidate = int(requested or 0)
        except (TypeError, ValueError):
            candidate = 0
        if candidate and candidate != int(task_id):
            row = connection.execute(
                "SELECT id FROM tasks WHERE id = ? AND id <> ?", (candidate, int(task_id))
            ).fetchone()
            if row is not None:
                return int(row["id"])
        target = cls._normalize_summary(summary)
        if not target:
            return 0
        rows = connection.execute(
            """
            SELECT id, summary FROM tasks
            WHERE id <> ? AND status NOT IN ('dismissed', 'expired')
            ORDER BY id DESC LIMIT 200
            """,
            (int(task_id),),
        ).fetchall()
        for item in rows:
            if cls._normalize_summary(item["summary"]) == target:
                return int(item["id"])
        return 0

    @staticmethod
    def _merge_task_groups(connection: sqlite3.Connection, original_id: int, groups: Any) -> list[str]:
        """把重复任务的来源群并入原始任务，返回合并后的群列表。"""
        if isinstance(groups, str):
            try:
                groups = json.loads(groups or "[]")
            except (json.JSONDecodeError, TypeError):
                groups = []
        incoming = [str(name) for name in (groups or []) if str(name).strip()]
        if not incoming:
            return []
        row = connection.execute(
            "SELECT groups FROM tasks WHERE id = ?", (int(original_id),)
        ).fetchone()
        if row is None:
            return []
        try:
            existing = json.loads(row["groups"] or "[]")
        except (json.JSONDecodeError, TypeError):
            existing = []
        previous = [str(name) for name in existing if str(name).strip()] if isinstance(existing, list) else []
        merged = list(previous)
        for name in incoming:
            if name not in merged:
                merged.append(name)
        if merged != previous:
            connection.execute(
                "UPDATE tasks SET groups=?, updated_at=? WHERE id=?",
                (json.dumps(merged, ensure_ascii=False), iso(now_local()), int(original_id)),
            )
        return merged

    def apply_task_action(self, task_id: int, action: str, *, detail: Any = "") -> bool:
        """执行确认/忽略/完成/重开/稍后提醒，并追加事件记录。"""
        action = str(action or "").strip().lower()
        stamp = iso(now_local())
        with self._connect() as connection:
            row = connection.execute("SELECT status FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
            if row is None:
                return False
            current = str(row["status"] or "open")
            if action == "confirm":
                if current not in {"candidate", "open"}:
                    return False
                connection.execute(
                    "UPDATE tasks SET status='open', confirmed_at=?, updated_at=? WHERE id=?",
                    (stamp, stamp, int(task_id)),
                )
                self._task_event(connection, task_id, "confirmed", detail, created_at=stamp)
                return True
            if action == "dismiss":
                if current not in {"candidate", "open"}:
                    return False
                connection.execute(
                    "UPDATE tasks SET status='dismissed', dismissed_at=?, updated_at=? WHERE id=?",
                    (stamp, stamp, int(task_id)),
                )
                self._task_event(connection, task_id, "dismissed", detail, created_at=stamp)
                return True
            if action == "done":
                if current not in {"open", "candidate"}:
                    return False
                connection.execute(
                    "UPDATE tasks SET status='done', done_at=?, updated_at=? WHERE id=?",
                    (stamp, stamp, int(task_id)),
                )
                self._task_event(connection, task_id, "done", detail, created_at=stamp)
                return True
            if action == "snooze":
                if current not in {"candidate", "open"}:
                    return False
                payload = dict(detail) if isinstance(detail, dict) else {}
                until = parse_iso(payload.get("until")) if payload.get("until") else None
                if not isinstance(until, dt.datetime):
                    until = now_local() + dt.timedelta(days=1)
                payload["until"] = iso(until)
                connection.execute(
                    "UPDATE tasks SET last_reminded_at=?, snooze_until=?, updated_at=? WHERE id=?",
                    (stamp, iso(until), stamp, int(task_id)),
                )
                self._task_event(connection, task_id, "snoozed", payload, created_at=stamp)
                return True
            if action == "reopen":
                if current not in {"done", "dismissed", "expired"}:
                    return False
                connection.execute(
                    "UPDATE tasks SET status='open', done_at='', confirmed_at=?, updated_at=? WHERE id=?",
                    (stamp, stamp, int(task_id)),
                )
                self._task_event(connection, task_id, "reopened", detail, created_at=stamp)
                return True
            raise ValueError(f"不支持的任务动作: {action}")

    def set_task_status(self, task_id: int, done: bool) -> bool:
        """兼容旧调用：true=完成，false=重新打开。"""
        return self.apply_task_action(task_id, "done" if done else "reopen")

    def task_stats(self) -> dict[str, int]:
        now = iso(now_local())
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(status = 'candidate'), 0) AS candidate,
                       COALESCE(SUM(status = 'open'), 0) AS open,
                       COALESCE(SUM(status = 'done'), 0) AS done,
                       COALESCE(SUM(status = 'dismissed'), 0) AS dismissed,
                       COALESCE(SUM(status = 'expired'), 0) AS expired,
                       COALESCE(SUM(status = 'open' AND deadline <> '' AND deadline < ?), 0) AS overdue
                FROM tasks
                """,
                (now,),
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "candidate": int(row["candidate"] or 0),
            "open": int(row["open"] or 0),
            "done": int(row["done"] or 0),
            "dismissed": int(row["dismissed"] or 0),
            "expired": int(row["expired"] or 0),
            "overdue": int(row["overdue"] or 0),
        }

    def mark_task_reminded(self, task_id: int, *, detail: Any = "", when: Any = None) -> bool:
        stamp = iso(when or now_local())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks SET last_reminded_at=?, remind_count=remind_count+1, updated_at=?
                WHERE id=? AND status IN ('open', 'candidate')
                """,
                (stamp, stamp, int(task_id)),
            )
            changed = bool(cursor.rowcount)
            if changed:
                self._task_event(connection, task_id, "reminded", detail, created_at=stamp)
        return changed

    def task_metrics(self, *, start: str, end: str) -> dict[str, int | float]:
        with self._connect() as connection:
            def scalar(sql: str, params: tuple[Any, ...] = ()) -> int:
                row = connection.execute(sql, params).fetchone()
                return int(row[0] or 0)

            candidates = scalar(
                "SELECT COUNT(*) FROM task_events WHERE event='candidate_detected' AND created_at>=? AND created_at<?",
                (start, end),
            )
            confirmed = scalar(
                "SELECT COUNT(*) FROM task_events WHERE event='confirmed' AND created_at>=? AND created_at<?",
                (start, end),
            )
            dismissed = scalar(
                "SELECT COUNT(*) FROM task_events WHERE event='dismissed' AND created_at>=? AND created_at<?",
                (start, end),
            )
            completions = scalar(
                "SELECT COUNT(*) FROM task_events WHERE event='done' AND created_at>=? AND created_at<?",
                (start, end),
            )
            reminders = scalar(
                "SELECT COUNT(*) FROM task_events WHERE event='reminded' AND created_at>=? AND created_at<?",
                (start, end),
            )
            completions_after_reminder = scalar(
                """
                SELECT COUNT(DISTINCT d.task_id)
                FROM task_events d
                WHERE d.event='done' AND d.created_at>=? AND d.created_at<?
                  AND EXISTS (
                      SELECT 1 FROM task_events r
                      WHERE r.task_id=d.task_id AND r.event='reminded' AND r.created_at<=d.created_at
                  )
                """,
                (start, end),
            )
            overdue = scalar(
                "SELECT COUNT(*) FROM tasks WHERE status='open' AND deadline<>'' AND deadline<?",
                (end,),
            )
        return {
            "candidates": candidates,
            "confirmed": confirmed,
            "dismissed": dismissed,
            "completions": completions,
            "reminders": reminders,
            "completions_after_reminder": completions_after_reminder,
            "overdue": overdue,
            "confirmation_rate": round(confirmed * 100 / candidates, 1) if candidates else 0.0,
            "dismissal_rate": round(dismissed * 100 / candidates, 1) if candidates else 0.0,
            "completion_rate": round(completions * 100 / max(1, completions + overdue), 1),
        }

    def message_metrics(self, *, start: str, end: str = "", top: int = 5) -> dict[str, Any]:
        """窗口内的入库消息量：总数 / 涉及群数 / 消息最多的几个群（Roadmap A32）。

        只出计数，不出正文与发送者——面板要能贴给别人看。
        """
        where = "received_at >= ?"
        params: list[Any] = [start]
        if end:
            where += " AND received_at < ?"
            params.append(end)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS total, COUNT(DISTINCT group_id) AS groups "
                f"FROM messages WHERE {where}",
                tuple(params),
            ).fetchone()
            rows = connection.execute(
                f"SELECT group_id, COUNT(*) AS n FROM messages WHERE {where} "
                "GROUP BY group_id ORDER BY n DESC LIMIT ?",
                (*params, max(0, int(top))),
            ).fetchall()
        return {
            "total": int(row["total"] or 0),
            "distinct_groups": int(row["groups"] or 0),
            "top_groups": [
                {"group_id": str(item["group_id"] or ""), "messages": int(item["n"] or 0)}
                for item in rows
            ],
        }

    def delivery_metrics(self, *, start: str, end: str = "") -> dict[str, int]:
        """窗口内的投递结果（按 updated_at 归窗）：成功 / 失败 / 待发（Roadmap A32）。"""
        where = "updated_at >= ?"
        params: list[Any] = [start]
        if end:
            where += " AND updated_at < ?"
            params.append(end)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT status, COUNT(*) AS n FROM deliveries WHERE {where} GROUP BY status",
                tuple(params),
            ).fetchall()
        data = {str(item["status"]): int(item["n"] or 0) for item in rows}
        return {
            "sent": data.get("sent", 0),
            "pending": data.get("pending", 0),
            "failed": data.get("failed", 0),
            "total": sum(data.values()),
        }

    def delivery_stats(self) -> dict[str, int]:
        """投递队列概况，用于 /health 观测推送是否堆积。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS n FROM deliveries GROUP BY status"
            ).fetchall()
        data = {str(row["status"]): int(row["n"] or 0) for row in rows}
        return {
            "sent": data.get("sent", 0),
            "pending": data.get("pending", 0),
            "failed": data.get("failed", 0),
            "total": sum(data.values()),
        }

    def correction_stats(self, *, days: int = 30) -> dict[str, Any]:
        """按群和纠错类型汇总人工纠错，供规则反馈使用。"""
        since = iso(now_local() - dt.timedelta(days=max(1, int(days))))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT e.detail AS detail, t.groups AS groups
                FROM task_events e JOIN tasks t ON t.id = e.task_id
                WHERE e.event = 'corrected' AND e.created_at >= ?
                ORDER BY e.id DESC
                """,
                (since,),
            ).fetchall()
        total = 0
        by_type: dict[str, int] = {}
        by_group: dict[str, dict[str, int]] = {}
        for row in rows:
            try:
                detail = json.loads(row["detail"] or "{}")
            except (json.JSONDecodeError, TypeError):
                detail = {}
            kind = str(detail.get("type") or "unknown")
            total += 1
            by_type[kind] = by_type.get(kind, 0) + 1
            try:
                groups = json.loads(row["groups"] or "[]")
            except (json.JSONDecodeError, TypeError):
                groups = []
            for name in groups if isinstance(groups, list) else []:
                entry = by_group.setdefault(str(name), {})
                entry[kind] = entry.get(kind, 0) + 1
                entry["total"] = entry.get("total", 0) + 1
        return {"days": int(days), "total": total, "by_type": by_type, "by_group": by_group}

    def correction_insights(self, *, days: int = 30, min_group_hits: int = 3) -> list[str]:
        """把纠错样本转成可读的规则建议；只提建议，不自动改配置。"""
        stats = self.correction_stats(days=days)
        threshold = max(2, int(min_group_hits))
        insights: list[str] = []
        ranked = sorted(
            (stats.get("by_group") or {}).items(),
            key=lambda item: -int(item[1].get("total") or 0),
        )
        for name, entry in ranked:
            total = int(entry.get("total") or 0)
            if total < threshold:
                continue
            urgent_miss = int(entry.get("not_urgent") or 0)
            noise = int(entry.get("not_notice") or 0) + int(entry.get("not_task") or 0)
            duplicate = int(entry.get("duplicate") or 0)
            reasons: list[str] = []
            if urgent_miss:
                reasons.append(f"误判紧急 {urgent_miss} 次")
            if noise:
                reasons.append(f"误判为通知/待办 {noise} 次")
            if duplicate:
                reasons.append(f"重复 {duplicate} 次")
            if reasons:
                insights.append(
                    f"{name}：{'、'.join(reasons)}，建议收紧该群规则或加入低优先级群。"
                )
        if int((stats.get("by_type") or {}).get("duplicate") or 0) >= 3:
            insights.append("重复标记偏多，建议复查跨群去重时间窗 QQ_DIGEST_DEDUPE_HOURS。")
        return insights

    def feedback_summary(self, *, days: int = 30) -> dict[str, Any]:
        """人工反馈回收：候选 → 确认 / 忽略 / 纠错，外加按群纠错提示（A8）。

        只汇总与解释，不自动改配置——「怎么改规则」仍由人决定。
        """
        days = max(1, int(days))
        end = now_local()
        start = end - dt.timedelta(days=days)
        end = end + dt.timedelta(seconds=1)  # 窗口用 `< end`，补 1 秒，刚发生的反馈别被边界挡掉
        metrics = self.task_metrics(start=iso(start), end=iso(end))
        corrections = self.correction_stats(days=days)
        return {
            "days": days,
            "window_start": iso(start),
            "window_end": iso(end),
            "candidates": int(metrics.get("candidates") or 0),
            "confirmed": int(metrics.get("confirmed") or 0),
            "dismissed": int(metrics.get("dismissed") or 0),
            "corrected": int(corrections.get("total") or 0),
            "confirmation_rate": float(metrics.get("confirmation_rate") or 0.0),
            "dismissal_rate": float(metrics.get("dismissal_rate") or 0.0),
            "by_type": dict(corrections.get("by_type") or {}),
            "by_group": dict(corrections.get("by_group") or {}),
            "insights": self.correction_insights(days=days),
        }

    def clear_tasks(self) -> int:
        with self._connect() as connection:
            connection.execute("DELETE FROM task_events")
            cursor = connection.execute("DELETE FROM tasks")
            removed = cursor.rowcount
        return int(removed)

    def meta_get(self, key: str, default: str = "") -> str:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def meta_set(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    # ------------------------------------------------------------------- 维护
    def prune(self, *, retention_days: int | None = None) -> int:
        days = int(retention_days or self.retention_days)
        cutoff = iso(now_local() - dt.timedelta(days=days))
        with self._connect() as connection:
            # 先删子表（processed 外键引用 messages），再删父表
            connection.execute(
                "DELETE FROM processed WHERE msg_id IN (SELECT msg_id FROM messages WHERE received_at < ?)",
                (cutoff,),
            )
            cursor = connection.execute("DELETE FROM messages WHERE received_at < ?", (cutoff,))
            removed = cursor.rowcount or 0
            connection.execute("DELETE FROM processed WHERE msg_id NOT IN (SELECT msg_id FROM messages)")
            connection.execute("DELETE FROM decisions WHERE created_at < ?", (cutoff,))
            connection.execute("DELETE FROM llm_calls WHERE created_at < ?", (cutoff,))
        return removed

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            def scalar(sql: str, *params: Any) -> int:
                row = connection.execute(sql, params).fetchone()
                return int(row[0] or 0)

            return {
                "messages": scalar("SELECT COUNT(*) FROM messages"),
                "unprocessed": scalar(
                    "SELECT COUNT(*) FROM messages m LEFT JOIN processed p ON p.msg_id=m.msg_id WHERE p.msg_id IS NULL"
                ),
                "digests": scalar("SELECT COUNT(*) FROM digests"),
                "decisions": scalar("SELECT COUNT(*) FROM decisions"),
                "decisions_deferred": scalar(
                    "SELECT COUNT(*) FROM decisions WHERE outcome = ?", decisions.DEFERRED
                ),
                "llm_calls": scalar("SELECT COUNT(*) FROM llm_calls"),
                "llm_calls_failed": scalar(
                    "SELECT COUNT(*) FROM llm_calls WHERE status = ?", llmstats.STATUS_ERROR
                ),
                "deliveries_sent": scalar("SELECT COUNT(*) FROM deliveries WHERE status='sent'"),
                "deliveries_failed": scalar("SELECT COUNT(*) FROM deliveries WHERE status='failed'"),
            }

    def apply_task_correction(self, task_id: int, correction: str, *, value: Any = "") -> bool:
        """记录用户纠错，并按纠错类型修正任务状态或字段。"""
        correction = str(correction or "").strip().lower()
        allowed = {"not_notice", "not_task", "not_urgent", "duplicate", "category", "deadline", "clear_deadline"}
        if correction not in allowed:
            raise ValueError(f"不支持的纠错类型: {correction}")
        stamp = iso(now_local())
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, category, importance, deadline, summary, groups FROM tasks WHERE id = ?",
                (int(task_id),),
            ).fetchone()
            if row is None:
                return False
            previous_status = str(row["status"] or "open")
            previous_category = str(row["category"] or "info")
            previous_importance = int(row["importance"] or 3)
            previous_deadline = str(row["deadline"] or "")
            new_status = previous_status
            new_category = previous_category
            new_importance = previous_importance
            new_deadline = previous_deadline
            duplicate_of = 0
            if correction in {"not_notice", "not_task"}:
                new_status = "dismissed"
            elif correction == "duplicate":
                new_status = "dismissed"
                duplicate_of = self._resolve_duplicate_target(
                    connection,
                    task_id=int(task_id),
                    summary=str(row["summary"] or ""),
                    requested=value,
                )
            elif correction == "not_urgent":
                new_category = "action" if previous_category == "urgent" else previous_category
                new_importance = min(previous_importance, 3)
            elif correction == "category":
                new_category = str(value or "").strip().lower()
                if new_category not in {"urgent", "action", "academic", "info"}:
                    raise ValueError("无效的任务分类")
                new_importance = {"urgent": 5, "action": 3, "academic": 3, "info": 1}[new_category]
            elif correction == "deadline":
                parsed_deadline = iso(value)
                if str(value or "").strip() and not parsed_deadline:
                    raise ValueError("无效的截止时间")
                new_deadline = parsed_deadline
            elif correction == "clear_deadline":
                new_deadline = ""
            detail = {
                "type": correction,
                "previous": {
                    "status": previous_status,
                    "category": previous_category,
                    "importance": previous_importance,
                    "deadline": previous_deadline,
                },
                "new": {
                    "status": new_status,
                    "category": new_category,
                    "importance": new_importance,
                    "deadline": new_deadline,
                    "duplicate_of": duplicate_of,
                },
                "value": str(value or "")[:500],
            }
            dismissed_at = stamp if new_status == "dismissed" and previous_status != "dismissed" else ""
            connection.execute(
                """
                UPDATE tasks SET
                    status=?, category=?, importance=?, deadline=?, duplicate_of=?,
                    dismissed_at=CASE WHEN ? <> '' THEN ? ELSE dismissed_at END,
                    updated_at=?
                WHERE id=?
                """,
                (
                    new_status,
                    new_category,
                    new_importance,
                    new_deadline,
                    int(duplicate_of),
                    dismissed_at,
                    dismissed_at,
                    stamp,
                    int(task_id),
                ),
            )
            if duplicate_of:
                merged_groups = self._merge_task_groups(connection, duplicate_of, row["groups"])
                if merged_groups:
                    detail["merged_groups"] = merged_groups
            self._task_event(connection, task_id, "corrected", detail, created_at=stamp)
            return True
