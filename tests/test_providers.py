"""模型 Provider 抽象（Roadmap A4）回归测试。

盯三件事：业务层不再自己拼 HTTP、Provider 真的可替换（注册表 + 注入假实现）、
以及 `LLMResult` 真的带回模型名与 token 用量（A5 成本统计要用的数据）。
"""

from __future__ import annotations

import datetime as dt
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import qq_digest  # noqa: E402
from qq_digest import Message  # noqa: E402
from qq_live_digest import providers  # noqa: E402
from qq_live_digest.config import DEFAULT_DASHSCOPE_ENDPOINT, Settings  # noqa: E402
from qq_live_digest.retry import LLMError, LLMRequestError, LLMResponseError  # noqa: E402


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def completion_body(
    content: str,
    *,
    model: str = "qwen-plus",
    prompt: int = 12,
    completion: int = 7,
) -> FakeResponse:
    payload = {
        "model": model,
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }
    return FakeResponse(json.dumps(payload).encode("utf-8"))


class FakeProvider(providers.LLMProvider):
    """测试用假 Provider：不联网，按预设载荷回应，并记录调用参数。"""

    name = "fake"

    def __init__(self, payload=None, *, text: str = "", model: str = "fake-model") -> None:
        super().__init__(model=model)
        self.payload = payload
        self.text = text
        self.calls: list[dict] = []

    def complete(self, messages, *, temperature=0.1, max_tokens=3000, timeout=None):
        self.calls.append({"messages": list(messages), "temperature": temperature})
        text = self.text or json.dumps(self.payload, ensure_ascii=False)
        return providers.LLMResult(
            text=text,
            provider=self.name,
            model=self.model,
            prompt_tokens=10,
            completion_tokens=5,
            latency_ms=3,
        )


class JsonExtractionTest(unittest.TestCase):
    def test_plain_object(self) -> None:
        self.assertEqual(providers.extract_json_object('{"a": 1}'), {"a": 1})

    def test_fenced_and_wrapped_object(self) -> None:
        text = '好的，结果如下：\n```json\n{"items": [{"id": 1}]}\n```\n以上。'
        self.assertEqual(providers.extract_json_object(text), {"items": [{"id": 1}]})

    def test_garbage_raises_response_error(self) -> None:
        for text in ("", "没有 JSON", "[1, 2, 3]"):
            with self.subTest(text=text):
                with self.assertRaises(LLMResponseError):
                    providers.extract_json_object(text)

    def test_malformed_json_raises_response_error(self) -> None:
        with self.assertRaises(LLMResponseError):
            providers.extract_json_object("{不是 JSON}")


class ResultTest(unittest.TestCase):
    def test_total_tokens_sums_both_sides(self) -> None:
        result = providers.LLMResult("x", prompt_tokens=120, completion_tokens=30)
        self.assertEqual(result.total_tokens, 150)


class BuildProviderTest(unittest.TestCase):
    def test_default_is_openai_compat(self) -> None:
        settings = Settings(dashscope_api_key="k")
        provider = providers.build_provider(settings)
        self.assertIsInstance(provider, providers.OpenAICompatProvider)
        self.assertEqual(provider.model, settings.dashscope_model)
        self.assertEqual(provider.endpoint, DEFAULT_DASHSCOPE_ENDPOINT)

    def test_model_override_wins(self) -> None:
        provider = providers.build_provider(Settings(dashscope_api_key="k"), model="qwen3-vl-plus")
        self.assertEqual(provider.model, "qwen3-vl-plus")

    def test_missing_key_falls_back_to_null(self) -> None:
        provider = providers.build_provider(Settings())
        self.assertIsInstance(provider, providers.NullProvider)
        with self.assertRaises(LLMError):
            provider.complete([{"role": "user", "content": "hi"}])

    def test_explicit_none_provider_stays_offline(self) -> None:
        settings = Settings(dashscope_api_key="k", llm_provider="none")
        self.assertIsInstance(providers.build_provider(settings), providers.NullProvider)

    def test_unknown_provider_raises(self) -> None:
        settings = Settings(dashscope_api_key="k", llm_provider="no-such-vendor")
        with self.assertRaises(LLMError):
            providers.build_provider(settings)

    def test_describe_has_no_secret(self) -> None:
        provider = providers.build_provider(Settings(dashscope_api_key="super-secret"))
        self.assertEqual(set(provider.describe()), {"provider", "model", "timeout"})
        self.assertNotIn("super-secret", json.dumps(provider.describe()))


class OpenAICompatTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = providers.OpenAICompatProvider(
            api_key="key", endpoint="https://example.invalid/v1/chat/completions", model="qwen-plus"
        )

    def test_payload_and_usage_round_trip(self) -> None:
        seen: dict = {}

        def fake_urlopen(request, timeout=0):
            seen["payload"] = json.loads(request.data.decode("utf-8"))
            seen["headers"] = dict(request.headers)
            seen["timeout"] = timeout
            return completion_body("你好", model="qwen-plus-2026")

        with mock.patch.object(providers.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = self.provider.complete([{"role": "user", "content": "hi"}], max_tokens=800)

        self.assertEqual(seen["payload"]["model"], "qwen-plus")
        self.assertEqual(seen["payload"]["max_tokens"], 800)
        self.assertEqual(seen["payload"]["messages"], [{"role": "user", "content": "hi"}])
        self.assertNotIn("enable_thinking", seen["payload"])
        self.assertEqual(seen["headers"]["Authorization"], "Bearer key")
        self.assertEqual(result.text, "你好")
        self.assertEqual(result.model, "qwen-plus-2026")
        self.assertEqual(result.provider, "openai-compat")
        self.assertEqual(result.total_tokens, 19)
        self.assertGreaterEqual(result.latency_ms, 0)

    def test_qwen3_disables_thinking(self) -> None:
        provider = self.provider.for_model("qwen3.8-max")
        payload = provider.build_payload([{"role": "user", "content": "x"}], temperature=0.1, max_tokens=10)
        self.assertFalse(payload["enable_thinking"])

    def test_http_error_keeps_status(self) -> None:
        error = urllib.error.HTTPError(
            "https://example.invalid", 429, "too many", {}, io.BytesIO(b"slow down")
        )
        with mock.patch.object(providers.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(LLMRequestError) as ctx:
                self.provider.complete([{"role": "user", "content": "x"}])
        self.assertEqual(ctx.exception.status, 429)

    def test_network_error_is_zero_status(self) -> None:
        with mock.patch.object(
            providers.urllib.request, "urlopen", side_effect=urllib.error.URLError("boom")
        ):
            with self.assertRaises(LLMRequestError) as ctx:
                self.provider.complete([{"role": "user", "content": "x"}])
        self.assertEqual(ctx.exception.status, 0)

    def test_non_json_body_is_response_error(self) -> None:
        with mock.patch.object(
            providers.urllib.request, "urlopen", return_value=FakeResponse(b"<html>502</html>")
        ):
            with self.assertRaises(LLMResponseError):
                self.provider.complete([{"role": "user", "content": "x"}])

    def test_missing_choices_is_response_error(self) -> None:
        with mock.patch.object(
            providers.urllib.request, "urlopen", return_value=FakeResponse(b'{"model": "qwen-plus"}')
        ):
            with self.assertRaises(LLMResponseError):
                self.provider.complete([{"role": "user", "content": "x"}])

    def test_missing_key_fails_before_network(self) -> None:
        provider = providers.OpenAICompatProvider(api_key="", endpoint="https://example.invalid")
        with mock.patch.object(providers.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(LLMError):
                provider.complete([{"role": "user", "content": "x"}])
        urlopen.assert_not_called()


class RetryPolicyTest(unittest.TestCase):
    def test_transient_error_is_retried_then_succeeds(self) -> None:
        calls: list[int] = []

        class Flaky(providers.LLMProvider):
            name = "flaky"

            def complete(self, messages, *, temperature=0.1, max_tokens=3000, timeout=None):
                calls.append(1)
                if len(calls) == 1:
                    raise LLMRequestError("rate limited", status=429)
                return providers.LLMResult("ok", model="m")

        result = providers.complete_with_retries(
            Flaky(), [{"role": "user", "content": "x"}], retries=2, backoff=0.0
        )
        self.assertEqual(result.text, "ok")
        self.assertEqual(len(calls), 2)

    def test_auth_error_is_not_retried(self) -> None:
        calls: list[int] = []

        class Denied(providers.LLMProvider):
            name = "denied"

            def complete(self, messages, *, temperature=0.1, max_tokens=3000, timeout=None):
                calls.append(1)
                raise LLMRequestError("HTTP 401", status=401)

        with self.assertRaises(LLMRequestError):
            providers.complete_with_retries(Denied(), [{"role": "user", "content": "x"}], retries=3, backoff=0.0)
        self.assertEqual(len(calls), 1)

    def test_bad_json_is_retried(self) -> None:
        provider = FakeProvider(text="这不是 JSON")
        with self.assertRaises(LLMResponseError):
            providers.complete_json_with_retries(
                provider, [{"role": "user", "content": "x"}], retries=1, backoff=0.0
            )
        self.assertEqual(len(provider.calls), 2)


class RegistryTest(unittest.TestCase):
    def test_openai_compat_is_registered(self) -> None:
        self.assertIn("openai-compat", providers.provider_names())

    def test_blank_name_rejected(self) -> None:
        with self.assertRaises(ValueError):
            providers.register_provider("  ", lambda settings, model="": providers.NullProvider())

    def test_duplicate_needs_replace(self) -> None:
        factory = lambda settings, model="": providers.NullProvider()  # noqa: E731
        providers.register_provider("test-vendor", factory)
        try:
            with self.assertRaises(ValueError):
                providers.register_provider("test-vendor", factory)
            providers.register_provider("test-vendor", factory, replace=True)
            settings = Settings(dashscope_api_key="k", llm_provider="test-vendor")
            self.assertIsInstance(providers.build_provider(settings), providers.NullProvider)
        finally:
            providers.PROVIDERS.pop("test-vendor", None)


class ForModelTest(unittest.TestCase):
    def test_returns_copy_and_keeps_original(self) -> None:
        provider = providers.OpenAICompatProvider(api_key="k", endpoint="https://example.invalid", model="qwen-plus")
        vision = provider.for_model("qwen3-vl-plus")
        self.assertIsNot(vision, provider)
        self.assertEqual(vision.model, "qwen3-vl-plus")
        self.assertEqual(provider.model, "qwen-plus")
        self.assertEqual(vision.api_key, "k")


def item(index: int, text: str) -> dict:
    return {
        "message": Message(timestamp=dt.datetime(2026, 10, 1, 9, 0), sender="班长", text=text),
        "category": "info",
        "score": 5,
    }


class RefineItemsTest(unittest.TestCase):
    def test_provider_output_drives_refinement(self) -> None:
        payload = {
            "items": [
                {"id": 1, "keep": False},
                {
                    "id": 2,
                    "category": "urgent",
                    "importance": 5,
                    "summary": "今晚 18:00 前交报名表",
                    "audience": "已报名同学",
                    "condition": "",
                    "action": "交报名表",
                    "details": ["只针对已报名同学"],
                    "deadline": "2026-10-01 18:00",
                },
            ]
        }
        provider = FakeProvider(payload)
        items = [item(1, "哈哈哈"), item(2, "请已报名的同学今晚18:00前交报名表")]

        result = qq_digest.refine_items(items, provider)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["category"], "urgent")
        self.assertEqual(result[0]["importance"], 5)
        self.assertEqual(result[0]["summary"], "今晚 18:00 前交报名表")
        self.assertEqual(result[0]["action"], "交报名表")
        self.assertEqual(result[0]["details"], ["只针对已报名同学"])
        self.assertEqual(result[0]["deadline_dt"], dt.datetime(2026, 10, 1, 18, 0))
        self.assertIn("今晚18:00前交报名表", provider.calls[0]["messages"][1]["content"])

    def test_tail_beyond_fifty_is_kept_verbatim(self) -> None:
        payload = {"items": [{"id": index, "keep": True, "summary": f"条 {index}"} for index in range(1, 51)]}
        items = [item(index, f"通知 {index}") for index in range(1, 56)]
        result = qq_digest.refine_items(items, FakeProvider(payload))
        self.assertEqual(len(result), 55)
        self.assertNotIn("summary", result[-1])

    def test_empty_items_never_calls_provider(self) -> None:
        provider = FakeProvider({"items": []})
        self.assertEqual(qq_digest.refine_items([], provider), [])
        self.assertEqual(provider.calls, [])

    def test_provider_failure_propagates_as_llm_error(self) -> None:
        class Broken(providers.LLMProvider):
            name = "broken"

            def complete(self, messages, *, temperature=0.1, max_tokens=3000, timeout=None):
                raise LLMRequestError("connection refused", status=0)

        with self.assertRaises(LLMError):
            qq_digest.refine_items([item(1, "通知")], Broken(), retries=0, backoff=0.0)

    def test_legacy_wrapper_still_works(self) -> None:
        payload = {"items": [{"id": 1, "keep": True, "summary": "改好了"}]}
        with mock.patch.object(
            providers.urllib.request, "urlopen", return_value=completion_body(json.dumps(payload))
        ) as urlopen:
            result = qq_digest.refine_with_dashscope(
                [item(1, "原始通知")],
                "key",
                "qwen-plus",
                "https://example.invalid/v1/chat/completions",
                30,
                retries=0,
            )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["summary"], "改好了")
        self.assertEqual(urlopen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
