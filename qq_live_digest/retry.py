"""外部调用重试：只重试可恢复错误，避免把配置错误重试放大。"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
from typing import Callable, TypeVar

LOGGER = logging.getLogger(__name__)

T = TypeVar("T")

TRANSIENT_HTTP_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class LLMError(RuntimeError):
    """大模型调用失败（重试耗尽或不可重试）。"""


class LLMRequestError(LLMError):
    """HTTP 层失败；status 为 0 表示网络/超时错误。"""

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = int(status)


class LLMResponseError(LLMError):
    """请求成功但响应无法解析成预期结构。"""


def is_transient_http(status: int) -> bool:
    return int(status) in TRANSIENT_HTTP_CODES


def llm_should_retry(error: BaseException) -> bool:
    """限流/5xx/网络超时/响应格式异常值得重试，鉴权和参数错误不值得。"""
    if isinstance(error, urllib.error.HTTPError):
        return is_transient_http(error.code)
    if isinstance(error, LLMRequestError):
        return error.status == 0 or is_transient_http(error.status)
    return isinstance(
        error,
        (
            LLMResponseError,
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            OSError,
            json.JSONDecodeError,
        ),
    )


def call_with_retries(
    func: Callable[[], T],
    *,
    retries: int = 2,
    backoff: float = 1.5,
    should_retry: Callable[[BaseException], bool] | None = None,
    logger: logging.Logger | None = None,
    label: str = "外部调用",
    on_attempt: Callable[[int, T | None, BaseException | None], None] | None = None,
) -> T:
    """执行 func；可恢复错误按指数退避重试，其余错误立刻抛出。

    `on_attempt(次数, 结果, 异常)` 在每次尝试后回调，供用量统计（A5）记录每次请求；
    回调自身抛出的异常不会影响重试逻辑。
    """
    attempts = max(1, int(retries) + 1)
    log = logger or LOGGER
    allow = should_retry or (lambda _error: True)
    last: Exception | None = None

    def _notify(number: int, value: T | None, error: BaseException | None) -> None:
        if on_attempt is None:
            return
        try:
            on_attempt(number, value, error)
        except Exception:  # noqa: BLE001 - 统计失败不能影响主流程
            log.debug("重试回调异常", exc_info=True)

    for attempt in range(attempts):
        if attempt:
            delay = max(0.0, float(backoff)) * (2 ** (attempt - 1))
            if delay:
                time.sleep(delay)
        try:
            value = func()
        except Exception as error:  # noqa: BLE001 - 是否重试交给 should_retry 判定
            last = error
            _notify(attempt + 1, None, error)
            if attempt >= attempts - 1 or not allow(error):
                raise
            log.warning("%s第 %d/%d 次失败，准备重试：%s", label, attempt + 1, attempts, error)
        else:
            _notify(attempt + 1, value, None)
            return value
    raise LLMError(f"{label}失败：{last}")
