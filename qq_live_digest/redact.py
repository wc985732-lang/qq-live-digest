"""脱敏小工具：群号 / ID 掩码（可观测面板与 doctor 共用同一份规则）。

规则：短号整段遮掉（避免短号被还原），够长的只留首尾各 2 位；空值返回调用方指定的占位文案。
"""

from __future__ import annotations

from typing import Any

MASK_MIN_LENGTH = 7  # 长度不足这个值就整段遮掉


def mask_id(value: Any, *, empty: str = "") -> str:
    text = str(value or "")
    if not text:
        return empty
    if len(text) < MASK_MIN_LENGTH:
        return "*" * len(text)
    return f"{text[:2]}***{text[-2:]}"


__all__ = ["MASK_MIN_LENGTH", "mask_id"]
