"""消息处理可观测面板（Roadmap A32）回归测试。

盯四件事：

1. 聚合本身算得对：过滤率只按已决结论算、候选量含待投递、推送成功率不看待发；
2. **脱敏是真的**：面板输出里出现不了原始群号、正文、发送者；
3. 三个出口（CLI / `/panel` / `/api/panel`）看到的是同一份数字；
4. doctor 有一行摘要，且统计失败不会把自检搞崩。

全程不联网、不碰真实 `data/`。
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
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402
from qq_live_digest import llmstats, observe, providers  # noqa: E402
from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.doctor import OK, DoctorContext, check_panel  # noqa: E402
from qq_live_digest.providers import LLMCall  # noqa: E402
from qq_live_digest.store import Store  # noqa: E402
from qq_live_digest.webapp import TaskWebServer  # noqa: E402

GROUP_A = "123456789"
GROUP_B = "987654321"


def build_store(path: Path) -> Store:
    """造一窗「两群、五条消息、六条已决结论」的数据（时间都落在最近一小时）。"""
    store = Store(path)
    now = dt.datetime.now()
    for index in range(5):
        store.insert_message(
            msg_id=f"m{index}",
            group_id=GROUP_A if index < 3 else GROUP_B,
            content=f"第 {index} 条消息正文",
            received_at=now - dt.timedelta(minutes=30),
        )
    store.record_decisions(
        [
            {"msg_id": "m0", "group_id": GROUP_A, "outcome": "filtered", "reason": "分值低"},
            {"msg_id": "m1", "group_id": GROUP_A, "outcome": "filtered", "reason": "闲聊"},
            {"msg_id": "m2", "group_id": GROUP_A, "outcome": "filtered", "reason": "闲聊"},
            {"msg_id": "m3", "group_id": GROUP_B, "outcome": "deduped", "reason": "重复"},
            {"msg_id": "m4", "group_id": GROUP_B, "outcome": "pushed", "reason": "进了摘要"},
            {"msg_id": "m5", "group_id": GROUP_B, "outcome": "held", "reason": "暂无通道"},
            {"msg_id": "m6", "group_id": GROUP_B, "outcome": "pending", "reason": "等投递"},
        ]
    )
    store.add_llm_call(
        LLMCall(
            purpose=llmstats.PURPOSE_REFINE,
            provider="openai-compat",
            model="qwen-plus",
            prompt_tokens=1000,
            completion_tokens=200,
            latency_ms=900,
        ),
        created_at=now - dt.timedelta(minutes=30),
    )
    store.add_llm_call(
        LLMCall(
            purpose=llmstats.PURPOSE_VISION,
            provider="openai-compat",
            model="qwen3-vl-plus",
            status=llmstats.STATUS_ERROR,
            error="LLMRequestError: 429 限流",
        ),
        created_at=now - dt.timedelta(minutes=20),
    )
    for key, ok in (("d1", True), ("d2", False)):
        _, delivery_id = store.claim_delivery(
            digest_id=1,
            channel="wxpusher",
            target="UID_demo",
            dedupe_key=key,
            max_attempts=3,
            retry_seconds=30,
        )
        store.mark_delivery(delivery_id, ok=ok, error="" if ok else "HTTP 502")
    confirmed = store.upsert_task(
        task_key="t1", summary="提交材料", category="action", status="candidate", confidence=0.4
    )
    store.apply_task_action(confirmed, "confirm")
    dismissed = store.upsert_task(
        task_key="t2", summary="可能不用管", category="action", status="candidate", confidence=0.3
    )
    store.apply_task_action(dismissed, "dismiss")
    return store


class PureHelpersTest(unittest.TestCase):
    def test_clamp_days_keeps_the_view_inside_a_sane_range(self):
        self.assertEqual(observe.clamp_days(7), 7)
        self.assertEqual(observe.clamp_days(0), 1)
        self.assertEqual(observe.clamp_days(999), observe.MAX_DAYS)
        self.assertEqual(observe.clamp_days("abc"), observe.DEFAULT_DAYS)
        self.assertEqual(observe.clamp_days(None), observe.DEFAULT_DAYS)

    def test_view_label_names_the_preset_windows(self):
        self.assertEqual(observe.view_label(1), "按日")
        self.assertEqual(observe.view_label(7), "按周")
        self.assertEqual(observe.view_label(30), "按月")
        self.assertEqual(observe.view_label(3), "最近 3 天")

    def test_mask_id_hides_the_middle_of_a_group_number(self):
        self.assertEqual(observe.mask_id(GROUP_A), "12***89")
        self.assertEqual(observe.mask_id("1234"), "****")
        self.assertEqual(observe.mask_id(""), "（无群号）")

    def test_rate_never_pretends_to_be_full_marks_when_empty(self):
        self.assertEqual(observe.rate(0, 0), 0.0)
        self.assertEqual(observe.rate(1, 3), 33.3)
        self.assertEqual(observe.rate(2, 2), 100.0)

    def test_filter_rate_counts_only_decided_outcomes(self):
        counts = {"filtered": 3, "deduped": 1, "pushed": 1, "held": 1, "pending": 9}
        self.assertEqual(observe.filter_rate(counts), 50.0)
        self.assertEqual(observe.filter_rate({}), 0.0)

    def test_push_rate_ignores_still_pending_deliveries(self):
        self.assertEqual(observe.push_rate({"sent": 3, "failed": 1, "pending": 5}), 75.0)
        self.assertEqual(observe.push_rate({"sent": 0, "failed": 0, "pending": 2}), 0.0)


class SnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = build_store(Path(self.tmp.name) / "panel.sqlite3")

    def test_snapshot_aggregates_the_window(self) -> None:
        snap = observe.snapshot(self.store, days=1)
        self.assertEqual(snap["days"], 1)
        self.assertEqual(snap["view"], "按日")
        self.assertEqual(snap["messages"]["total"], 5)
        self.assertEqual(snap["messages"]["distinct_groups"], 2)
        self.assertEqual(snap["decided"], 6)
        self.assertEqual(snap["filter_rate"], 50.0)
        self.assertEqual(snap["candidates"], 3)
        self.assertEqual(snap["push_rate"], 50.0)
        self.assertEqual(snap["llm"]["calls"], 2)
        self.assertEqual(snap["llm"]["failed"], 1)
        self.assertEqual(snap["tasks"]["candidates"], 2)
        self.assertEqual(snap["tasks"]["confirmed"], 1)

    def test_window_is_respected(self) -> None:
        self.assertEqual(observe.snapshot(self.store, days=1)["messages"]["total"], 5)
        snap = observe.snapshot(self.store, days=365)
        self.assertEqual(snap["messages"]["total"], 5)
        old = dt.datetime.now() - dt.timedelta(days=40)
        self.store.insert_message(
            msg_id="old", group_id=GROUP_A, content="上个月的", received_at=old
        )
        self.assertEqual(observe.snapshot(self.store, days=1)["messages"]["total"], 5)
        self.assertEqual(observe.snapshot(self.store, days=365)["messages"]["total"], 6)

    def test_payload_is_json_ready(self) -> None:
        data = observe.payload(observe.snapshot(self.store, days=1))
        text = json.dumps(data, ensure_ascii=False)
        self.assertEqual(data["messages"]["total"], 5)
        self.assertEqual(data["decisions"]["decided"], 6)
        self.assertEqual(data["llm"]["tokens"], 1200)
        self.assertEqual(data["push"]["rate"], 50.0)
        json.loads(text)

    def test_payload_and_text_never_leak_raw_group_ids(self) -> None:
        snap = observe.snapshot(self.store, days=1)
        text = observe.render_text(snap)
        blob = json.dumps(observe.payload(snap), ensure_ascii=False) + text
        self.assertNotIn(GROUP_A, blob)
        self.assertNotIn(GROUP_B, blob)
        self.assertNotIn("消息正文", blob)
        self.assertIn("12***89", text)
        self.assertIn("消息处理面板", text)
        self.assertIn("过滤率 50.0%", text)
        self.assertIn("成功率 50.0%", text)

    def test_headline_is_a_one_liner_for_doctor(self) -> None:
        line = observe.headline(observe.snapshot(self.store, days=7))
        self.assertIn("7 天收到 5 条", line)
        self.assertIn("过滤率 50.0%", line)
        self.assertNotIn(GROUP_A, line)

    def test_empty_store_renders_zeros_instead_of_crashing(self) -> None:
        empty = Store(Path(self.tmp.name) / "empty.sqlite3")
        snap = observe.snapshot(empty, days=1)
        text = observe.render_text(snap)
        self.assertIn("收：0 条", text)
        self.assertIn("过滤率 0.0%", text)
        self.assertNotIn("群消息 TOP", text)


class ObserveCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.env_file = self.data_dir / "test.env"
        self.env_file.write_text(
            f"QQ_DIGEST_DATA_DIR={self.data_dir.as_posix()}\n"
            f"QQ_DIGEST_LOG_DIR={(self.data_dir / 'logs').as_posix()}\n"
            f"QQ_DIGEST_GROUPS={GROUP_A},{GROUP_B}\n",
            encoding="utf-8",
        )
        self._saved = {
            key: os.environ.get(key) for key in ("QQ_DIGEST_DATA_DIR", "QQ_DIGEST_LOG_DIR")
        }
        self.addCleanup(self._restore_env)
        self.addCleanup(self._close_log_handlers)
        self.addCleanup(lambda: providers.set_call_recorder(None))
        build_store(self.data_dir / "digest.sqlite3")

    def _restore_env(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _close_log_handlers(self) -> None:
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()

    def _run(self, *extra: str) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main.main(["--env", str(self.env_file), "observe", *extra])
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_text_view_shows_the_panel(self) -> None:
        text = self._run("--days", "1")
        self.assertIn("消息处理面板 · 按日", text)
        self.assertIn("收：5 条 · 涉及 2 个群", text)
        self.assertIn("过滤率 50.0%", text)
        self.assertIn("12***89", text)
        self.assertNotIn(GROUP_A, text)

    def test_json_view_is_machine_readable(self) -> None:
        payload = json.loads(self._run("--days", "1", "--json"))
        self.assertEqual(payload["messages"]["total"], 5)
        self.assertEqual(payload["push"]["sent"], 1)
        self.assertEqual(payload["push"]["failed"], 1)
        self.assertIn("filter_rate", payload["decisions"])

    def test_days_flag_is_clamped(self) -> None:
        self.assertIn("按日", self._run("--days", "0"))
        self.assertIn(f"最近 {observe.MAX_DAYS} 天", self._run("--days", "400"))


class PanelApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = build_store(Path(self.tmp.name) / "api.sqlite3")
        self.settings = Settings(
            group_whitelist=(GROUP_A, GROUP_B),
            web_host="127.0.0.1",
            web_port=0,
            web_token="secret",
        )
        self.server = TaskWebServer(self.settings, self.store)
        self.assertTrue(self.server.start())
        self.addCleanup(self.server.stop)
        assert self.server.server is not None
        self.base = f"http://127.0.0.1:{self.server.server.server_address[1]}"

    def _get(self, path: str, token: str = "") -> str:
        headers = {"X-Token": token} if token else {}
        request = urllib.request.Request(self.base + path, headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read().decode("utf-8")

    def test_panel_api_matches_the_cli_numbers(self) -> None:
        payload = json.loads(self._get("/api/panel?token=secret&days=1"))
        self.assertEqual(payload["messages"]["total"], 5)
        self.assertEqual(payload["decisions"]["filter_rate"], 50.0)
        self.assertEqual(payload["push"]["rate"], 50.0)
        self.assertNotIn(GROUP_A, json.dumps(payload, ensure_ascii=False))

    def test_panel_api_requires_the_token(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as context:
            self._get("/api/panel")
        self.assertEqual(context.exception.code, 401)

    def test_panel_page_loads_the_api(self) -> None:
        html = self._get("/panel?token=secret")
        self.assertIn("消息处理面板", html)
        self.assertIn("/api/panel", html)


class DoctorPanelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _context(self, store: Store) -> DoctorContext:
        return DoctorContext(
            settings=Settings(group_whitelist=(GROUP_A,)),
            open_store=lambda: store,
        )

    def test_reports_the_window_summary(self) -> None:
        store = build_store(self.dir / "d.sqlite3")
        check = check_panel(self._context(store))
        self.assertEqual(check.status, OK)
        self.assertIn("过滤率 50.0%", check.detail)
        self.assertIn("main.py observe", check.hint)

    def test_says_so_when_nothing_arrived(self) -> None:
        store = Store(self.dir / "empty.sqlite3")
        check = check_panel(self._context(store))
        self.assertIn("没有消息入库", check.detail)
        self.assertIn("main.py observe", check.hint)

    def test_a_broken_store_warns_instead_of_raising(self) -> None:
        class Broken:
            def message_metrics(self, **kwargs):
                raise RuntimeError("库读不出来")

        check = check_panel(self._context(Broken()))  # type: ignore[arg-type]
        self.assertEqual(check.status, "WARN")
        self.assertIn("无法统计", check.detail)


if __name__ == "__main__":
    unittest.main()
