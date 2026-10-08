"""Token / 成本统计（Roadmap A5）回归测试。

盯四件事：Provider 层把每次调用（含重试 / 失败 / 降级 / 跳过）都落成**一行**、
store 能按时间区间聚合、日 / 周 / 月视图的纯函数算得对、CLI 与 doctor 真的把数字显示出来。
全程不联网，也不依赖真实 `data/`。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402
from qq_live_digest import llmstats, providers, summarizer  # noqa: E402
from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.doctor import DoctorContext, check_llm_usage  # noqa: E402
from qq_live_digest.providers import LLMCall, LLMResult  # noqa: E402
from qq_live_digest.retry import LLMRequestError, LLMResponseError  # noqa: E402
from qq_live_digest.store import Store  # noqa: E402
from qq_live_digest.timeutil import iso  # noqa: E402

NOW = dt.datetime(2026, 10, 9, 12, 0, 0)
NOTICE = "【学院通知】关于2026年秋季学期选课工作的通知"


def make_result(
    text: str = "ok",
    *,
    prompt: int = 10,
    completion: int = 5,
    latency: int = 42,
    model: str = "fake-model",
) -> LLMResult:
    return LLMResult(
        text=text,
        provider="scripted",
        model=model,
        prompt_tokens=prompt,
        completion_tokens=completion,
        latency_ms=latency,
    )


class ScriptedProvider(providers.LLMProvider):
    """按脚本回答的假 Provider：弹出预设的 `LLMResult` 或抛预设异常，不联网。"""

    name = "scripted"

    def __init__(self, script: list, *, model: str = "fake-model") -> None:
        super().__init__(model=model)
        self.script = list(script)
        self.calls: list[list] = []

    def complete(self, messages, *, temperature=0.1, max_tokens=3000, timeout=None):
        self.calls.append(list(messages))
        item = self.script.pop(0) if self.script else LLMResult(text="ok", provider="scripted")
        if isinstance(item, BaseException):
            raise item
        return item


def make_record(msg_id: str = "m1", content: str = NOTICE, *, minutes_ago: int = 11) -> dict:
    stamp = NOW - dt.timedelta(minutes=minutes_ago)
    return {
        "msg_id": msg_id,
        "source": "qqbot",
        "event": "GROUP_MESSAGE_CREATE",
        "group_id": "g1",
        "group_name": "学院通知群",
        "sender_id": "u1",
        "sender_name": "辅导员",
        "ts": iso(stamp),
        "received_at": iso(stamp),
        "content": content,
    }


# ------------------------------------------------------------------ Provider 落库
class CallRecordingTest(unittest.TestCase):
    """Provider 层是唯一知道「这次到底花没花 token」的地方，必须记录准确。"""

    def setUp(self) -> None:
        self.calls: list[LLMCall] = []
        providers.set_call_recorder(self.calls.append)
        self.addCleanup(lambda: providers.set_call_recorder(None))

    def test_success_records_single_row_with_usage(self) -> None:
        provider = ScriptedProvider([make_result()])
        result = providers.complete_with_retries(
            provider, [{"role": "user", "content": "hi"}], purpose=llmstats.PURPOSE_REFINE
        )
        self.assertEqual(result.text, "ok")
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call.status, llmstats.STATUS_OK)
        self.assertEqual(call.purpose, llmstats.PURPOSE_REFINE)
        self.assertEqual((call.provider, call.model), ("scripted", "fake-model"))
        self.assertEqual((call.prompt_tokens, call.completion_tokens), (10, 5))
        self.assertEqual(call.total_tokens, 15)
        self.assertEqual((call.attempts, call.retried, call.fallback), (1, False, False))
        self.assertEqual(call.error, "")

    def test_retry_then_success_records_attempts_and_final_tokens(self) -> None:
        provider = ScriptedProvider([LLMRequestError("busy", status=503), make_result(prompt=7)])
        providers.complete_with_retries(provider, [{"role": "user", "content": "hi"}], retries=1, backoff=0)
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call.status, llmstats.STATUS_OK)
        self.assertEqual(call.attempts, 2)
        self.assertTrue(call.retried)
        self.assertFalse(call.fallback)
        self.assertEqual(call.prompt_tokens, 7)
        self.assertEqual(len(provider.calls), 2)

    def test_exhausted_retries_records_failure_and_downgrade(self) -> None:
        provider = ScriptedProvider(
            [LLMRequestError("rate limited", status=429), LLMRequestError("rate limited", status=429)]
        )
        with self.assertRaises(LLMRequestError):
            providers.complete_with_retries(provider, [{"role": "user", "content": "hi"}], retries=1, backoff=0)
        call = self.calls[0]
        self.assertEqual(call.status, llmstats.STATUS_ERROR)
        self.assertEqual(call.attempts, 2)
        self.assertTrue(call.retried)
        self.assertTrue(call.fallback)
        self.assertIn("rate limited", call.error)
        self.assertIn("LLMRequestError", call.error)
        self.assertEqual(call.prompt_tokens, 0)

    def test_non_retryable_error_is_recorded_once(self) -> None:
        provider = ScriptedProvider([LLMRequestError("bad key", status=401)])
        with self.assertRaises(LLMRequestError):
            providers.complete_with_retries(provider, [{"role": "user", "content": "hi"}], retries=2, backoff=0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0].attempts, 1)
        self.assertFalse(self.calls[0].retried)
        self.assertEqual(len(provider.calls), 1)

    def test_json_call_records_usage_of_successful_attempt(self) -> None:
        payload = json.dumps({"items": [{"id": 1, "summary": "选课通知"}]})
        provider = ScriptedProvider(
            [LLMResponseError("not json"), make_result(payload, prompt=33, completion=11)]
        )
        data = providers.complete_json_with_retries(
            provider,
            [{"role": "user", "content": "hi"}],
            retries=1,
            backoff=0,
            purpose=llmstats.PURPOSE_REFINE,
        )
        self.assertEqual(data["items"][0]["summary"], "选课通知")
        call = self.calls[0]
        self.assertEqual(call.status, llmstats.STATUS_OK)
        self.assertEqual((call.prompt_tokens, call.completion_tokens), (33, 11))
        self.assertEqual(call.attempts, 2)

    def test_complete_json_still_returns_only_the_object(self) -> None:
        provider = ScriptedProvider([make_result('{"a": 1}')])
        self.assertEqual(provider.complete_json([{"role": "user", "content": "hi"}]), {"a": 1})

    def test_recorder_failure_never_breaks_the_call(self) -> None:
        providers.set_call_recorder(mock.Mock(side_effect=RuntimeError("db down")))
        provider = ScriptedProvider([make_result()])
        with self.assertLogs("qq_live_digest.providers", level="WARNING") as captured:
            result = providers.complete_with_retries(provider, [{"role": "user", "content": "hi"}])
        self.assertEqual(result.text, "ok")
        self.assertIn("写入模型用量失败", "\n".join(captured.output))

    def test_without_recorder_nothing_happens(self) -> None:
        providers.set_call_recorder(None)
        provider = ScriptedProvider([make_result()])
        providers.complete_with_retries(provider, [{"role": "user", "content": "hi"}])
        self.assertEqual(self.calls, [])

    def test_record_skip_marks_it_as_skipped(self) -> None:
        providers.record_skip(purpose=llmstats.PURPOSE_REFINE, reason="未配置密钥", model="qwen-plus")
        call = self.calls[0]
        self.assertEqual(call.status, llmstats.STATUS_SKIPPED)
        self.assertEqual(call.attempts, 0)
        self.assertTrue(call.fallback)
        self.assertEqual(call.provider, providers.NULL_PROVIDER)
        self.assertEqual(call.error, "未配置密钥")


# ------------------------------------------------------------------ store 聚合
class StoreUsageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "usage.sqlite3")

    def _call(self, *, status="ok", created_at=None, purpose="refine", prompt=100, completion=50) -> LLMCall:
        return LLMCall(
            purpose=purpose,
            provider="openai-compat",
            model="qwen-plus",
            status=status,
            prompt_tokens=prompt,
            completion_tokens=completion,
            latency_ms=120,
            attempts=2 if status == "ok" else 1,
            retried=status == "ok",
            fallback=status != "ok",
            error="" if status == "ok" else "LLMRequestError: 429",
        )

    def test_add_llm_call_round_trips_fields(self) -> None:
        row_id = self.store.add_llm_call(self._call(), created_at=NOW)
        self.assertGreater(row_id, 0)
        row = self.store.recent_llm_calls(limit=1)[0]
        self.assertEqual(row["purpose"], "refine")
        self.assertEqual(row["model"], "qwen-plus")
        self.assertEqual(row["prompt_tokens"], 100)
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["retried"], 1)
        self.assertEqual(row["fallback"], 0)
        self.assertEqual(row["created_at"], iso(NOW))

    def test_window_filter_only_returns_recent_rows(self) -> None:
        self.store.add_llm_call(self._call(), created_at=NOW - dt.timedelta(days=3))
        self.store.add_llm_call(self._call(), created_at=NOW - dt.timedelta(hours=1))
        rows = self.store.llm_calls_since(NOW - dt.timedelta(days=1))
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(self.store.llm_calls_between(NOW - dt.timedelta(days=4), NOW)), 2)

    def test_summary_counts_success_failure_and_tokens(self) -> None:
        self.store.add_llm_call(self._call(), created_at=NOW)
        self.store.add_llm_call(self._call(status="error", prompt=0, completion=0), created_at=NOW)
        self.store.add_llm_call(
            LLMCall(status=llmstats.STATUS_SKIPPED, attempts=0, fallback=True, error="未配置密钥"),
            created_at=NOW,
        )
        summary = self.store.llm_call_summary(hours=24)
        self.assertEqual(summary["calls"], 3)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["retried"], 1)
        self.assertEqual(summary["prompt_tokens"], 100)

    def test_counts_expose_usage_table(self) -> None:
        self.store.add_llm_call(self._call(), created_at=NOW)
        self.store.add_llm_call(self._call(status="error"), created_at=NOW)
        counts = self.store.counts()
        self.assertEqual(counts["llm_calls"], 2)
        self.assertEqual(counts["llm_calls_failed"], 1)

    def test_prune_drops_expired_rows(self) -> None:
        self.store.add_llm_call(self._call(), created_at=dt.datetime.now() - dt.timedelta(days=10))
        self.store.add_llm_call(self._call(), created_at=dt.datetime.now())
        self.store.prune(retention_days=2)
        self.assertEqual(self.store.counts()["llm_calls"], 1)


# ------------------------------------------------------------------ 纯函数视图
class LlmStatsPureTest(unittest.TestCase):
    def test_parse_period_accepts_aliases_and_rejects_junk(self) -> None:
        self.assertEqual(llmstats.parse_period("W"), llmstats.PERIOD_WEEK)
        self.assertEqual(llmstats.parse_period("月"), llmstats.PERIOD_MONTH)
        self.assertEqual(llmstats.parse_period("day"), llmstats.PERIOD_DAY)
        with self.assertRaises(ValueError):
            llmstats.parse_period("quarter")

    def test_bucket_key_uses_iso_week_and_month(self) -> None:
        moment = dt.datetime(2026, 10, 9, 15, 30)
        self.assertEqual(llmstats.bucket_key(moment, llmstats.PERIOD_DAY), "2026-10-09")
        self.assertEqual(llmstats.bucket_key(moment, llmstats.PERIOD_WEEK), "2026-W41")
        self.assertEqual(llmstats.bucket_key(moment, llmstats.PERIOD_MONTH), "2026-10")

    def test_recent_keys_cross_month_boundary(self) -> None:
        keys = llmstats.recent_keys(
            llmstats.PERIOD_DAY, 3, now=dt.datetime(2026, 1, 2, 9, 0)
        )
        self.assertEqual(keys, ["2025-12-31", "2026-01-01", "2026-01-02"])

    def test_recent_keys_for_months_walk_back_a_year(self) -> None:
        keys = llmstats.recent_keys(
            llmstats.PERIOD_MONTH, 4, now=dt.datetime(2026, 2, 15)
        )
        self.assertEqual(keys, ["2025-11", "2025-12", "2026-01", "2026-02"])

    def test_window_start_points_at_monday_for_weeks(self) -> None:
        start = llmstats.window_start(
            llmstats.PERIOD_WEEK, 2, now=dt.datetime(2026, 10, 9, 15, 0)
        )
        self.assertEqual(start, dt.datetime(2026, 9, 28, 0, 0))

    def test_group_rows_fills_gaps_and_ignores_old_rows(self) -> None:
        rows = [
            {"created_at": iso(dt.datetime(2026, 10, 9, 9, 0)), "status": "ok",
             "prompt_tokens": 1000, "completion_tokens": 500, "latency_ms": 300, "purpose": "refine",
             "model": "qwen-plus", "retried": 1, "fallback": 0},
            {"created_at": iso(dt.datetime(2026, 10, 8, 9, 0)), "status": "error",
             "prompt_tokens": 0, "completion_tokens": 0, "latency_ms": 0, "purpose": "refine",
             "model": "qwen-plus", "retried": 0, "fallback": 1},
            {"created_at": iso(dt.datetime(2026, 1, 1, 9, 0)), "status": "ok",
             "prompt_tokens": 99, "completion_tokens": 99, "latency_ms": 1, "purpose": "refine",
             "model": "qwen-plus", "retried": 0, "fallback": 0},
        ]
        buckets = llmstats.group_rows(rows, llmstats.PERIOD_DAY, now=NOW, count=3)
        self.assertEqual([bucket["key"] for bucket in buckets], ["2026-10-07", "2026-10-08", "2026-10-09"])
        self.assertEqual(llmstats.bucket_tokens(buckets[0]), 0)   # 空桶也要显示出来
        self.assertEqual(buckets[0]["calls"], 0)
        self.assertEqual(buckets[1]["failed"], 1)
        self.assertEqual(llmstats.bucket_tokens(buckets[2]), 1500)
        self.assertEqual(buckets[2]["retried"], 1)

    def test_totals_and_cost_and_formatting(self) -> None:
        rows = [
            {"created_at": iso(NOW), "status": "ok", "prompt_tokens": 1_000_000, "completion_tokens": 500_000,
             "latency_ms": 10, "purpose": "refine", "model": "qwen-plus", "retried": 0, "fallback": 0},
        ]
        totals = llmstats.totals_from_rows(rows)
        cost = llmstats.bucket_cost(totals, price_in=2.0, price_out=4.0)
        self.assertAlmostEqual(cost, 2.0 + 2.0, places=6)
        self.assertEqual(llmstats.format_tokens(1500), "1.5k")
        self.assertEqual(llmstats.format_tokens(2_500_000), "2.50M")
        self.assertEqual(llmstats.format_cost(0), "¥0")
        self.assertEqual(llmstats.format_cost(0.5), "¥0.500")
        self.assertEqual(llmstats.format_cost(3.456), "¥3.46")
        self.assertIn("输入 1.00M / 输出 500.0k token", llmstats.summarise_bucket(totals, price_in=2.0, price_out=4.0))

    def test_top_errors_orders_by_frequency(self) -> None:
        rows = [
            {"status": "error", "error": "429 限流"},
            {"status": "error", "error": "429 限流"},
            {"status": "error", "error": "超时"},
            {"status": "ok", "error": ""},
        ]
        self.assertEqual(llmstats.top_errors(rows, limit=3), [("429 限流", 2), ("超时", 1)])

    def test_render_table_has_header_and_one_row_per_bucket(self) -> None:
        buckets = llmstats.group_rows([], llmstats.PERIOD_DAY, now=NOW, count=3)
        lines = llmstats.render_table(buckets, price_in=1.0, price_out=2.0)
        self.assertEqual(len(lines), len(buckets) + 1)
        self.assertIn("费用", lines[0])
        self.assertIn("¥0", lines[1])
        self.assertIn("2026-10-09", lines[-1])

    def test_describe_calls_explains_status_and_reason(self) -> None:
        rows = [
            {"created_at": iso(NOW), "status": "error", "purpose": "vision", "model": "qwen3-vl-plus",
             "prompt_tokens": 0, "completion_tokens": 0, "latency_ms": 800, "attempts": 2,
             "retried": 1, "fallback": 1, "error": "LLMRequestError: 429"},
        ]
        text = "\n".join(llmstats.describe_calls(rows))
        self.assertIn("[失败]", text)
        self.assertIn("图片识别", text)
        self.assertIn("尝试 2 次", text)
        self.assertIn("已降级", text)
        self.assertIn("LLMRequestError: 429", text)

    def test_breakdown_uses_labels_and_truncates(self) -> None:
        text = llmstats.breakdown(
            {"refine": 5, "vision": 2, "chat": 1}, labels=llmstats.PURPOSE_LABELS, limit=2
        )
        self.assertTrue(text.startswith("候选精炼 5、图片识别 2"))
        self.assertIn("其他 1 类", text)


# ------------------------------------------------------------------ 业务接线
class SummarizerWiringTest(unittest.TestCase):
    """业务层不用知道统计的存在：refine 走完链路，用量就该自己出现。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "wire.sqlite3")
        providers.set_call_recorder(self.store.add_llm_call)
        self.addCleanup(lambda: providers.set_call_recorder(None))

    def _settings(self, **overrides) -> Settings:
        values = dict(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            min_score=3,
            llm_enabled=True,
            dashscope_api_key="test-key",
            llm_max_retries=0,
            llm_retry_backoff=0.0,
        )
        values.update(overrides)
        return Settings(**values)

    def test_refine_call_is_recorded_with_refine_purpose(self) -> None:
        provider = ScriptedProvider([make_result(json.dumps({"items": [{"id": 1, "summary": "选课通知"}]}))])
        with mock.patch.object(providers, "build_provider", return_value=provider):
            digest = summarizer.build_digest(self._settings(), [make_record()], now=NOW)
        self.assertTrue(digest.llm_used)
        row = self.store.recent_llm_calls(limit=1)[0]
        self.assertEqual(row["purpose"], llmstats.PURPOSE_REFINE)
        self.assertEqual(row["status"], llmstats.STATUS_OK)
        self.assertEqual(row["prompt_tokens"], 10)
        self.assertGreater(row["latency_ms"] - 1, -1)

    def test_missing_key_is_recorded_as_skipped_not_silent(self) -> None:
        settings = self._settings(dashscope_api_key="")
        digest = summarizer.build_digest(settings, [make_record()], now=NOW)
        self.assertFalse(digest.llm_used)
        self.assertIn("DASHSCOPE_API_KEY", digest.llm_error)
        row = self.store.recent_llm_calls(limit=1)[0]
        self.assertEqual(row["status"], llmstats.STATUS_SKIPPED)
        self.assertIn("DASHSCOPE_API_KEY", row["error"])

    def test_model_failure_is_recorded_and_still_degrades_to_local_rules(self) -> None:
        provider = ScriptedProvider([LLMRequestError("boom", status=429)])
        with mock.patch.object(providers, "build_provider", return_value=provider):
            digest = summarizer.build_digest(self._settings(), [make_record()], now=NOW)
        self.assertFalse(digest.llm_used)
        row = self.store.recent_llm_calls(limit=1)[0]
        self.assertEqual(row["status"], llmstats.STATUS_ERROR)
        self.assertEqual(row["fallback"], 1)
        self.assertIn("boom", row["error"])


# ------------------------------------------------------------------ CLI / doctor
class LlmStatsCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.env_file = self.data_dir / "test.env"
        self.env_file.write_text(
            f"QQ_DIGEST_DATA_DIR={self.data_dir.as_posix()}\n"
            f"QQ_DIGEST_LOG_DIR={(self.data_dir / 'logs').as_posix()}\n",
            encoding="utf-8",
        )
        self._saved = {
            key: os.environ.get(key) for key in ("QQ_DIGEST_DATA_DIR", "QQ_DIGEST_LOG_DIR")
        }
        self.addCleanup(self._restore_env)
        self.addCleanup(self._close_log_handlers)
        # build_service 会把记录器指向这里的临时库；跑完必须摘掉，否则后面的测试
        # 会往一个已删除的目录里写用量。
        self.addCleanup(lambda: providers.set_call_recorder(None))
        self.store = Store(self.data_dir / "digest.sqlite3")
        self.store.add_llm_call(
            LLMCall(
                purpose=llmstats.PURPOSE_REFINE,
                provider="openai-compat",
                model="qwen-plus",
                prompt_tokens=1_000_000,
                completion_tokens=200_000,
                latency_ms=900,
            ),
            created_at=dt.datetime.now(),
        )
        self.store.add_llm_call(
            LLMCall(
                purpose=llmstats.PURPOSE_VISION,
                provider="openai-compat",
                model="qwen3-vl-plus",
                status=llmstats.STATUS_ERROR,
                attempts=2,
                retried=True,
                fallback=True,
                error="LLMRequestError: 429 限流",
            ),
            created_at=dt.datetime.now(),
        )

    def _restore_env(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _close_log_handlers(self) -> None:
        """CLI 会调 setup_logging 打开日志文件；Windows 上不关掉就删不掉临时目录。"""
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()

    def _run(self, *extra: str) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main.main(["--env", str(self.env_file), "llm-stats", *extra])
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_day_view_shows_tokens_cost_and_failures(self) -> None:
        text = self._run("--period", "day", "--recent", "5")
        self.assertIn("按日", text)
        self.assertIn("1.00M", text)
        self.assertIn("200.0k", text)
        self.assertIn("尝试 2 次", text)
        self.assertIn("429 限流", text)
        self.assertIn("候选精炼 1", text)
        self.assertNotIn("还没有用量记录", text)

    def test_missing_price_falls_back_to_tokens_only(self) -> None:
        text = self._run("--period", "day", "--buckets", "1")
        self.assertIn("单价未配置", text)
        self.assertIn("费用 ¥0", text)

    def test_week_and_month_views_render(self) -> None:
        self.assertIn("按周", self._run("--period", "week"))
        self.assertIn("按月", self._run("--period", "month"))

    def test_json_output_is_machine_readable(self) -> None:
        payload = json.loads(self._run("--json", "--buckets", "3"))
        self.assertEqual(payload["period"], "day")
        self.assertEqual(len(payload["buckets"]), 3)
        self.assertEqual(payload["totals"]["calls"], 2)
        self.assertEqual(payload["top_errors"][0][1], 1)
        self.assertEqual(payload["price"], {"in_per_million": 0.0, "out_per_million": 0.0})

    def test_bad_period_exits_with_usage_error(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main.main(["--env", str(self.env_file), "llm-stats", "--period", "quarter"])
        self.assertEqual(code, 2)
        self.assertIn("未知的周期", buffer.getvalue())


class DoctorUsageCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.store = Store(self.data_dir / "digest.sqlite3")

    def _context(self, **overrides) -> DoctorContext:
        settings = Settings(data_dir=self.data_dir, **overrides)
        return DoctorContext(settings=settings, open_store=lambda: self.store)

    def test_empty_table_is_a_plain_ok(self) -> None:
        check = check_llm_usage(self._context())
        self.assertEqual(check.status, "OK")
        self.assertIn("没有模型调用", check.detail)

    def test_failures_surface_as_warning_with_hint(self) -> None:
        self.store.add_llm_call(
            LLMCall(status=llmstats.STATUS_ERROR, prompt_tokens=1200, completion_tokens=300,
                    retried=True, fallback=True, error="LLMRequestError: 429"),
            created_at=dt.datetime.now(),
        )
        check = check_llm_usage(self._context(llm_price_in=2.0, llm_price_out=8.0))
        self.assertEqual(check.status, "WARN")
        self.assertIn("失败 1", check.detail)
        self.assertIn("¥0.0048", check.detail)
        self.assertIn("llm-stats", check.hint)

    def test_missing_price_returns_a_hint_not_a_warning(self) -> None:
        self.store.add_llm_call(LLMCall(prompt_tokens=100, completion_tokens=50), created_at=dt.datetime.now())
        check = check_llm_usage(self._context())
        self.assertEqual(check.status, "OK")
        self.assertIn("QQ_DIGEST_LLM_PRICE_IN", check.hint)


if __name__ == "__main__":
    unittest.main()
