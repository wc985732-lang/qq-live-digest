"""模型分级路由（Roadmap A6）回归测试。

盯三件事：

1. 路由决定本身是纯函数、可复现：谁走轻量、谁升级、谁干脆不调模型，理由说得清；
2. 决定要真的落到用量表（`llm_calls.route` / `route_reason`），并且经得住老库迁移；
3. 默认（没配轻量模型）行为与 A6 之前一致——不悄悄换模型、不悄悄省调用。

全程不联网，也不依赖真实 `data/`。
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest import llmstats, providers, routing, summarizer  # noqa: E402
from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.providers import LLMCall, LLMResult  # noqa: E402
from qq_live_digest.store import Store  # noqa: E402

NOW = dt.datetime(2026, 10, 9, 12, 0, 0)
NOTICE = "【学院通知】请各班于明天 12:00 前提交材料到教务系统，逾期不候"


def make_settings(**overrides) -> Settings:
    values = dict(
        group_whitelist=("g1",),
        group_aliases={"g1": "学院通知群"},
        min_score=3,
        dashscope_model="qwen-plus",
        dashscope_api_key="test-key",
        llm_enabled=True,
        llm_max_retries=0,
        llm_retry_backoff=0.0,
    )
    values.update(overrides)
    return Settings(**values)


def high_item(score: int = 6) -> dict:
    """高置信度候选：分值高出阈值 + 有截止时间 + 有行动 + 判紧急。"""
    return {
        "score": score,
        "deadline_dt": NOW + dt.timedelta(hours=20),
        "action": "提交材料",
        "urgent": ["今天"],
        "action_words": ["提交"],
        "evidence": "请各班于明天 12:00 前提交材料",
    }


def low_item(score: int = 3) -> dict:
    """低置信度候选：刚够阈值，没截止、没行动、不紧急。"""
    return {"score": score}


class DecideRouteTest(unittest.TestCase):
    def test_without_light_model_walks_the_default_model(self) -> None:
        decision = routing.decide_route([high_item()], make_settings(llm_model_light=""))
        self.assertEqual(decision.tier, llmstats.ROUTE_STRONG)
        self.assertEqual(decision.model, "qwen-plus")
        self.assertIn("未配置轻量模型", decision.reason)

    def test_easy_batch_goes_to_the_light_model(self) -> None:
        decision = routing.decide_route(
            [high_item()], make_settings(llm_model_light="qwen-turbo")
        )
        self.assertEqual(decision.tier, llmstats.ROUTE_LIGHT)
        self.assertEqual(decision.model, "qwen-turbo")
        self.assertIn("轻量模型", decision.reason)

    def test_too_many_candidates_escalate(self) -> None:
        settings = make_settings(llm_model_light="qwen-turbo", llm_route_max_light_items=1)
        decision = routing.decide_route([high_item(), high_item()], settings)
        self.assertEqual(decision.tier, llmstats.ROUTE_STRONG)
        self.assertEqual(decision.model, "qwen-plus")
        self.assertIn("上限", decision.reason)

    def test_borderline_score_escalates(self) -> None:
        settings = make_settings(llm_model_light="qwen-turbo")
        decision = routing.decide_route([low_item(score=4)], settings)
        self.assertEqual(decision.tier, llmstats.ROUTE_STRONG)
        self.assertIn("阈值", decision.reason)

    def test_low_confidence_escalates(self) -> None:
        settings = make_settings(llm_model_light="qwen-turbo")
        decision = routing.decide_route([low_item(score=9)], settings)
        self.assertEqual(decision.tier, llmstats.ROUTE_STRONG)
        self.assertIn("置信度", decision.reason)

    def test_easy_local_tier_is_opt_in(self) -> None:
        settings = make_settings(llm_model_light="qwen-turbo", llm_route_easy_local=True)
        decision = routing.decide_route([high_item()], settings)
        self.assertEqual(decision.tier, llmstats.ROUTE_RULE)
        self.assertEqual(decision.model, "")
        self.assertIn("本地规则", decision.reason)

    def test_easy_local_stays_off_by_default(self) -> None:
        settings = make_settings(llm_model_light="qwen-turbo")
        self.assertEqual(routing.decide_route([high_item()], settings).tier, llmstats.ROUTE_LIGHT)

    def test_easy_local_does_not_swallow_harder_batches(self) -> None:
        settings = make_settings(llm_model_light="qwen-turbo", llm_route_easy_local=True)
        decision = routing.decide_route([high_item(), low_item(score=9)], settings)
        self.assertEqual(decision.tier, llmstats.ROUTE_STRONG)

    def test_label_uses_the_llmstats_vocabulary(self) -> None:
        self.assertEqual(
            routing.RouteDecision(tier=llmstats.ROUTE_LIGHT).label,
            llmstats.ROUTE_LABELS[llmstats.ROUTE_LIGHT],
        )


class ScriptedProvider(providers.LLMProvider):
    """按脚本回答的假 Provider：不联网。"""

    name = "scripted"

    def __init__(self, script: list, *, model: str = "fake-model") -> None:
        super().__init__(model=model)
        self.script = list(script)

    def complete(self, messages, *, temperature=0.1, max_tokens=3000, timeout=None):
        item = self.script.pop(0) if self.script else LLMResult(text="ok", provider="scripted")
        if isinstance(item, BaseException):
            raise item
        return item


class RoutingPlumbingTest(unittest.TestCase):
    def test_complete_json_with_retries_records_the_route(self) -> None:
        recorded: list[LLMCall] = []
        providers.set_call_recorder(recorded.append)
        self.addCleanup(lambda: providers.set_call_recorder(None))
        provider = ScriptedProvider([LLMResult(text='{"items": []}', provider="scripted")])
        providers.complete_json_with_retries(
            provider,
            [{"role": "user", "content": "hi"}],
            retries=0,
            route=llmstats.ROUTE_LIGHT,
            route_reason="候选 1 条且都够清晰",
        )
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0].route, llmstats.ROUTE_LIGHT)
        self.assertIn("够清晰", recorded[0].route_reason)

    def test_skip_records_the_route_too(self) -> None:
        recorded: list[LLMCall] = []
        providers.set_call_recorder(recorded.append)
        self.addCleanup(lambda: providers.set_call_recorder(None))
        providers.record_skip(
            purpose=llmstats.PURPOSE_REFINE,
            reason="本地规则足够",
            route=llmstats.ROUTE_RULE,
        )
        self.assertEqual(recorded[0].route, llmstats.ROUTE_RULE)
        self.assertEqual(recorded[0].status, llmstats.STATUS_SKIPPED)

    def test_store_round_trip_and_route_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "route.sqlite3")
            store.add_llm_call(LLMCall(route=llmstats.ROUTE_STRONG, route_reason="难例"))
            rows = store.recent_llm_calls(limit=5)
        self.assertEqual(rows[0]["route"], llmstats.ROUTE_STRONG)
        self.assertEqual(rows[0]["route_reason"], "难例")
        self.assertEqual(llmstats.route_counts(rows), {llmstats.ROUTE_STRONG: 1})
        self.assertEqual(llmstats.route_counts([{"model": "x"}]), {llmstats.ROUTE_DEFAULT: 1})

    def test_old_table_gains_the_route_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE llm_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    purpose TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'ok',
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    retried INTEGER NOT NULL DEFAULT 0,
                    fallback INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.commit()
            connection.close()
            store = Store(path)  # init() 会补列
            store.add_llm_call(LLMCall(route=llmstats.ROUTE_LIGHT, route_reason="迁移后写入"))
            rows = store.recent_llm_calls(limit=1)
        self.assertEqual(rows[0]["route"], llmstats.ROUTE_LIGHT)
        self.assertEqual(rows[0]["route_reason"], "迁移后写入")


def make_record(msg_id: str = "m1", content: str = NOTICE) -> dict:
    stamp = NOW - dt.timedelta(minutes=11)
    return {
        "msg_id": msg_id,
        "source": "qqbot",
        "event": "GROUP_MESSAGE_CREATE",
        "group_id": "g1",
        "group_name": "学院通知群",
        "sender_id": "u1",
        "sender_name": "班长",
        "ts": stamp.strftime("%Y-%m-%dT%H:%M:%S"),
        "received_at": stamp.strftime("%Y-%m-%dT%H:%M:%S"),
        "content": content,
        "source_text": "",
    }


class SummarizerRouteTest(unittest.TestCase):
    """业务层拿到的是「结论 + 理由」，用量表里要能对上。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "route.sqlite3")
        providers.set_call_recorder(self.store.add_llm_call)
        self.addCleanup(lambda: providers.set_call_recorder(None))

    def _refine(self, settings: Settings, records: list[dict], provider: ScriptedProvider):
        reply = LLMResult(
            text=json.dumps({"items": [{"id": 1, "summary": "选课材料要交"}]}),
            provider="scripted",
        )
        provider.script = [reply]
        with mock.patch.object(providers, "build_provider", return_value=provider) as builder:
            digest = summarizer.build_digest(settings, records, now=NOW)
        return digest, builder

    def test_default_settings_keep_the_old_behaviour(self) -> None:
        digest, builder = self._refine(make_settings(), [make_record()], ScriptedProvider([]))
        self.assertTrue(digest.llm_used)
        self.assertEqual(builder.call_args.kwargs.get("model"), "qwen-plus")
        row = self.store.recent_llm_calls(limit=1)[0]
        self.assertEqual(row["route"], llmstats.ROUTE_STRONG)
        self.assertIn("未配置轻量模型", row["route_reason"])

    def test_light_tier_is_chosen_and_recorded(self) -> None:
        settings = make_settings(llm_model_light="qwen-turbo")
        digest, builder = self._refine(settings, [make_record()], ScriptedProvider([]))
        self.assertTrue(digest.llm_used)
        self.assertEqual(builder.call_args.kwargs.get("model"), "qwen-turbo")
        row = self.store.recent_llm_calls(limit=1)[0]
        self.assertEqual(row["route"], llmstats.ROUTE_LIGHT)
        self.assertIn("轻量模型", row["route_reason"])

    def test_hard_batch_escalates_in_the_real_pipeline(self) -> None:
        settings = make_settings(llm_model_light="qwen-turbo", llm_route_max_light_items=1)
        records = [
            make_record("m1", "【学院通知】请各班于明天 12:00 前提交材料到教务系统"),
            make_record("m2", "【活动报名】招募志愿者，请于本周五 23:59 前填写报名表"),
        ]
        digest, builder = self._refine(settings, records, ScriptedProvider([]))
        self.assertTrue(digest.llm_used)
        self.assertEqual(builder.call_args.kwargs.get("model"), "qwen-plus")
        row = self.store.recent_llm_calls(limit=1)[0]
        self.assertEqual(row["route"], llmstats.ROUTE_STRONG)

    def test_easy_local_skips_the_model_call(self) -> None:
        settings = make_settings(llm_model_light="qwen-turbo", llm_route_easy_local=True)
        with mock.patch.object(providers, "build_provider") as builder:
            digest = summarizer.build_digest(settings, [make_record()], now=NOW)
        builder.assert_not_called()
        self.assertFalse(digest.llm_used)
        row = self.store.recent_llm_calls(limit=1)[0]
        self.assertEqual(row["route"], llmstats.ROUTE_RULE)
        self.assertEqual(row["status"], llmstats.STATUS_SKIPPED)
        self.assertIn("本地规则", row["error"])


if __name__ == "__main__":
    unittest.main()
