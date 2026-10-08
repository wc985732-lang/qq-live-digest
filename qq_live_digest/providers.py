"""模型 Provider 抽象（Roadmap A4）。

业务层（`qq_digest` 的候选精炼、`attachments` 的文档/视觉理解）只依赖这里的接口，
不再自己拼 HTTP 请求。好处有三：

- 换供应商或换兼容端点只改一处，业务代码不动；
- 测试与 `main.py simulate` 可以注入假实现，完全不联网；
- 每次调用都带回 model 与 token 用量，供成本统计（A5）使用。

边界约定：**Provider 只负责「发出去、拿回来」**，重试与降级策略仍属于调用方
（`complete_with_retries` / `complete_json_with_retries` 是给调用方的默认实现）。
"""

from __future__ import annotations

import abc
import copy
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from . import llmstats
from .retry import (
    LLMError,
    LLMRequestError,
    LLMResponseError,
    call_with_retries,
    llm_should_retry,
)

LOGGER = logging.getLogger(__name__)

#: 配置里不写 `QQ_DIGEST_LLM_PROVIDER` 时的默认实现。
DEFAULT_PROVIDER = "openai-compat"
#: 显式关闭模型调用时用的名字（等价于「没有密钥」，调用即失败）。
NULL_PROVIDER = "none"

Messages = Sequence[Mapping[str, Any]]


@dataclass(frozen=True)
class LLMResult:
    """一次模型调用的结果：文本 + 用量 + 耗时。"""

    text: str
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0

    @property
    def total_tokens(self) -> int:
        return int(self.prompt_tokens) + int(self.completion_tokens)


@dataclass(frozen=True)
class LLMCall:
    """一次**逻辑**调用的用量记录（Roadmap A5）。

    一次逻辑调用 = 调用方发起的一次 `complete*_with_retries`。重试期间的每次尝试都算进
    `attempts`，但只落一行：`retried` 表示是否真的重试过，`fallback` 表示失败后调用方
    是否回退到了本地规则 / 跳过 AI 步骤，`error` 记录失败原因。
    """

    purpose: str = llmstats.PURPOSE_CHAT
    provider: str = ""
    model: str = ""
    status: str = llmstats.STATUS_OK
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    attempts: int = 1
    retried: bool = False
    fallback: bool = False
    error: str = ""

    @property
    def total_tokens(self) -> int:
        return int(self.prompt_tokens) + int(self.completion_tokens)


def extract_json_object(text: str) -> dict[str, Any]:
    """从模型输出里抠出第一个 JSON 对象（容忍代码块与前后说明文字）。"""
    match = re.search(r"\{.*\}", str(text or ""), re.S)
    if not match:
        raise LLMResponseError("模型未返回可解析的 JSON")
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as error:
        raise LLMResponseError(f"模型 JSON 解析失败: {error}") from error
    if not isinstance(payload, dict):
        raise LLMResponseError("模型返回的 JSON 不是对象")
    return payload


class LLMProvider(abc.ABC):
    """文本与视觉共用的入口；实现类只需实现 `complete`。"""

    name = "abstract"

    def __init__(self, *, model: str = "", timeout: int = 60) -> None:
        self.model = str(model or "")
        self.timeout = int(timeout)

    @abc.abstractmethod
    def complete(
        self,
        messages: Messages,
        *,
        temperature: float = 0.1,
        max_tokens: int = 3000,
        timeout: int | None = None,
    ) -> LLMResult:
        """发一次请求。失败时抛 `LLMError` 子类；**不在这里重试**。"""

    def complete_json(
        self,
        messages: Messages,
        *,
        temperature: float = 0.1,
        max_tokens: int = 3000,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """要求模型返回 JSON 对象；解析失败抛 `LLMResponseError`。"""
        return self.complete_json_result(
            messages, temperature=temperature, max_tokens=max_tokens, timeout=timeout
        )[0]

    def complete_json_result(
        self,
        messages: Messages,
        *,
        temperature: float = 0.1,
        max_tokens: int = 3000,
        timeout: int | None = None,
    ) -> tuple[dict[str, Any], LLMResult]:
        """同 `complete_json`，但把带用量的 `LLMResult` 一起交出来（成本统计要用）。"""
        result = self.complete(
            messages, temperature=temperature, max_tokens=max_tokens, timeout=timeout
        )
        return extract_json_object(result.text), result

    def for_model(self, model: str) -> "LLMProvider":
        """返回同一实现、换成指定模型的实例（如文本模型与视觉模型共用一条通道）。"""
        clone = copy.copy(self)
        clone.model = str(model or "")
        return clone

    def describe(self) -> dict[str, Any]:
        """给 `doctor` 与工具面板看的自述信息（不含密钥）。"""
        return {"provider": self.name, "model": self.model, "timeout": self.timeout}


class NullProvider(LLMProvider):
    """没配置模型（或显式关闭）时的空实现：调用直接失败，绝不静默编造结果。"""

    name = NULL_PROVIDER

    def complete(
        self,
        messages: Messages,
        *,
        temperature: float = 0.1,
        max_tokens: int = 3000,
        timeout: int | None = None,
    ) -> LLMResult:
        raise LLMError("未配置模型 Provider：请设置 DASHSCOPE_API_KEY，或改用其他 Provider")


class OpenAICompatProvider(LLMProvider):
    """任何 OpenAI 兼容的 `/chat/completions` 端点（百炼、OpenAI、Ollama、vLLM…）。"""

    name = "openai-compat"

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        model: str = "",
        timeout: int = 60,
    ) -> None:
        super().__init__(model=model, timeout=timeout)
        self.api_key = str(api_key or "")
        self.endpoint = str(endpoint or "")

    def build_payload(
        self,
        messages: Messages,
        *,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": list(messages),
        }
        # qwen3 系列默认吐出思考过程，摘要场景既慢又贵，显式关掉。
        if str(self.model).lower().startswith("qwen3"):
            payload["enable_thinking"] = False
        return payload

    def complete(
        self,
        messages: Messages,
        *,
        temperature: float = 0.1,
        max_tokens: int = 3000,
        timeout: int | None = None,
    ) -> LLMResult:
        if not self.api_key:
            raise LLMError("未配置模型 API Key")
        if not self.endpoint:
            raise LLMError("未配置模型端点")
        payload = self.build_payload(messages, temperature=temperature, max_tokens=max_tokens)
        started = time.monotonic()
        body = self.post(payload, int(timeout or self.timeout))
        latency_ms = int((time.monotonic() - started) * 1000)
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return LLMResult(
            text=str(_content_of(body) or "").strip(),
            provider=self.name,
            model=str(body.get("model") or self.model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=latency_ms,
        )

    def post(self, payload: Mapping[str, Any], timeout: int) -> dict[str, Any]:
        """发一次 HTTP；网络/协议错误统一转成 `LLMError` 子类。"""
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=max(15, int(timeout))) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise LLMRequestError(
                f"模型 HTTP {error.code}: {detail}", status=int(error.code)
            ) from error
        except json.JSONDecodeError as error:
            raise LLMResponseError(f"模型返回非 JSON：{error}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise LLMRequestError(f"模型请求失败：{error}") from error
        if not isinstance(body, dict):
            raise LLMResponseError("模型返回格式异常")
        return body


def _content_of(body: Mapping[str, Any]) -> str:
    try:
        return str(body["choices"][0]["message"]["content"] or "")  # type: ignore[index]
    except (KeyError, IndexError, TypeError) as error:
        raise LLMResponseError("模型返回格式异常") from error


# ------------------------------------------------------------------ 注册与构造
ProviderFactory = Callable[..., LLMProvider]

PROVIDERS: dict[str, ProviderFactory] = {}


def register_provider(name: str, factory: ProviderFactory, *, replace: bool = False) -> None:
    """注册一个 Provider 实现（第三方/本地模型只需一行）。"""
    key = str(name or "").strip().lower()
    if not key:
        raise ValueError("Provider 名字不能为空")
    if key in PROVIDERS and not replace:
        raise ValueError(f"Provider 已注册：{key}")
    PROVIDERS[key] = factory


def provider_names() -> list[str]:
    return sorted(PROVIDERS)


def provider_name(settings: Any) -> str:
    return str(getattr(settings, "llm_provider", "") or DEFAULT_PROVIDER).strip().lower()


def build_provider(settings: Any, *, model: str = "") -> LLMProvider:
    """按配置造一个 Provider；没有密钥时返回 `NullProvider`（调用即失败）。"""
    name = provider_name(settings)
    if name == NULL_PROVIDER:
        return NullProvider()
    factory = PROVIDERS.get(name)
    if factory is None:
        raise LLMError(f"未知的模型 Provider：{name}（可选：{'、'.join(provider_names())}）")
    if not str(getattr(settings, "dashscope_api_key", "") or "").strip():
        return NullProvider()
    return factory(settings, model=model)


def _openai_compat(settings: Any, *, model: str = "") -> LLMProvider:
    return OpenAICompatProvider(
        api_key=getattr(settings, "dashscope_api_key", ""),
        endpoint=getattr(settings, "dashscope_endpoint", ""),
        model=model or getattr(settings, "dashscope_model", ""),
        timeout=int(getattr(settings, "llm_timeout", 60) or 60),
    )


register_provider("openai-compat", _openai_compat)


# ------------------------------------------------------------------ 用量记录（A5）
#: 落库回调，由 `DigestService` 在构造时挂上 `store.add_llm_call`。
#: 默认不记录：库外调用、单元测试不会被牵连，也不依赖数据库是否可写。
_call_recorder: Callable[[LLMCall], None] | None = None


def set_call_recorder(recorder: Callable[[LLMCall], None] | None) -> None:
    """挂上 / 清空用量记录器。"""
    global _call_recorder
    _call_recorder = recorder


def get_call_recorder() -> Callable[[LLMCall], None] | None:
    return _call_recorder


def report_call(call: LLMCall) -> None:
    """把一条用量记录交给记录器；记录器出错只记日志，绝不影响摘要与推送。"""
    recorder = _call_recorder
    if recorder is None:
        return
    try:
        recorder(call)
    except Exception:  # noqa: BLE001 - 统计失败不能影响主流程
        LOGGER.warning("写入模型用量失败", exc_info=True)


def record_skip(*, purpose: str, reason: str, provider: str = "", model: str = "") -> None:
    """记录一次「本该调用模型却跳过了」（如没配密钥）：让成本面板能解释「为什么一条都没有」。"""
    report_call(
        LLMCall(
            purpose=purpose,
            provider=provider or NULL_PROVIDER,
            model=model,
            status=llmstats.STATUS_SKIPPED,
            attempts=0,
            fallback=True,
            error=str(reason or ""),
        )
    )


def _as_result(value: Any) -> LLMResult | None:
    """从一次尝试的返回值里取出 `LLMResult`（JSON 版返回的是 (数据, 结果) 元组）。"""
    if isinstance(value, LLMResult):
        return value
    if isinstance(value, tuple) and value and isinstance(value[-1], LLMResult):
        return value[-1]
    return None


class _CallTracker:
    """把重试过程中的每次尝试汇总成一条 `LLMCall`。"""

    def __init__(self, provider: LLMProvider, *, purpose: str, fallback_on_error: bool) -> None:
        self.provider = provider
        self.purpose = purpose
        self.fallback_on_error = bool(fallback_on_error)
        self.attempts = 0
        self.result: LLMResult | None = None
        self.error: BaseException | None = None
        self.latency_ms = 0
        self._clock = time.monotonic()

    def observe(self, number: int, value: Any, error: BaseException | None) -> None:
        now = time.monotonic()
        self.latency_ms += max(0, int((now - self._clock) * 1000))
        self._clock = now
        self.attempts = max(self.attempts, int(number))
        if error is None:
            # 后续尝试成功即视为整次调用成功，之前失败的记录不该再拖累状态判断。
            self.error = None
            self.result = _as_result(value)
        else:
            self.error = error

    def emit(self) -> None:
        result = self.result
        failed = self.error is not None
        report_call(
            LLMCall(
                purpose=self.purpose,
                provider=str(getattr(result, "provider", "") or getattr(self.provider, "name", "")),
                model=str(getattr(result, "model", "") or getattr(self.provider, "model", "")),
                status=llmstats.STATUS_ERROR if failed else llmstats.STATUS_OK,
                prompt_tokens=int(getattr(result, "prompt_tokens", 0) or 0),
                completion_tokens=int(getattr(result, "completion_tokens", 0) or 0),
                latency_ms=self.latency_ms if failed else int(getattr(result, "latency_ms", 0) or 0),
                attempts=max(1, self.attempts),
                retried=self.attempts > 1,
                fallback=bool(failed and self.fallback_on_error),
                error=f"{type(self.error).__name__}: {self.error}" if failed else "",
            )
        )


# ------------------------------------------------------------------ 重试包装
def complete_with_retries(
    provider: LLMProvider,
    messages: Messages,
    *,
    retries: int = 2,
    backoff: float = 1.5,
    label: str = "模型调用",
    purpose: str = llmstats.PURPOSE_CHAT,
    fallback_on_error: bool = True,
    **kwargs: Any,
) -> LLMResult:
    """按 `llm_should_retry` 判定重试的调用（限流/5xx/超时/格式异常才重试）。

    `purpose` 用于成本面板分类；`fallback_on_error` 表示失败后调用方有本地回退路径
    （用于区分「降级」与「直接失败」）。
    """
    tracker = _CallTracker(provider, purpose=purpose, fallback_on_error=fallback_on_error)
    try:
        result = call_with_retries(
            lambda: provider.complete(messages, **kwargs),
            retries=retries,
            backoff=backoff,
            should_retry=llm_should_retry,
            label=label,
            on_attempt=tracker.observe,
        )
    except BaseException:
        tracker.emit()
        raise
    tracker.emit()
    return result


def complete_json_with_retries(
    provider: LLMProvider,
    messages: Messages,
    *,
    retries: int = 2,
    backoff: float = 1.5,
    label: str = "模型调用",
    purpose: str = llmstats.PURPOSE_CHAT,
    fallback_on_error: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """要 JSON 的重试版：连「返回内容不是 JSON」也一起重试。用量同样落库。"""
    tracker = _CallTracker(provider, purpose=purpose, fallback_on_error=fallback_on_error)
    try:
        value = call_with_retries(
            lambda: provider.complete_json_result(messages, **kwargs),
            retries=retries,
            backoff=backoff,
            should_retry=llm_should_retry,
            label=label,
            on_attempt=tracker.observe,
        )
    except BaseException:
        tracker.emit()
        raise
    tracker.emit()
    data, _ = value
    return data
