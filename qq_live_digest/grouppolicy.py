"""群级个性化策略（Roadmap A9）。

每个群可以在全局配置之上覆盖少量开关；**没写的字段一律继承全局**，所以只改一个群
不会牵连别的群。判断本身是纯函数：不碰数据库、不联网，方便单测与解释。

| 字段 | 作用 | 继承自 |
| --- | --- | --- |
| `quiet` | 安静群：只留明确通知，不推普通讨论 | `QQ_DIGEST_QUIET_GROUPS` |
| `keywords` | 该群的额外关键词，命中即视为明确通知 | 全局词表 |
| `min_score` | 该群进摘要的最低分 | `QQ_DIGEST_MIN_SCORE` |
| `model` | 该群走哪一档模型：`rule` / `light` / `strong` / `default` | A6 路由判据 |
| `quiet_hours` | 该群免打扰时段（如 `22:00-07:00`），时段内该群按安静群处理 | 不继承：只在本群显式配置时生效 |

配置写在 `QQ_DIGEST_GROUP_POLICIES`（JSON），键可以是群号或群名：

```json
{"123456": {"quiet": true, "min_score": 5, "keywords": ["考试", "选课"]},
 "学院通知群": {"model": "light", "quiet_hours": "23:00-06:30"}}
```
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MODEL_DEFAULT = "default"
MODEL_RULE = "rule"
MODEL_LIGHT = "light"
MODEL_STRONG = "strong"
MODEL_CHOICES = (MODEL_DEFAULT, MODEL_RULE, MODEL_LIGHT, MODEL_STRONG)
MODEL_LABELS = {
    MODEL_DEFAULT: "按路由判据",
    MODEL_RULE: "本地规则",
    MODEL_LIGHT: "轻量模型",
    MODEL_STRONG: "高能力模型",
}

FIELDS = ("quiet", "keywords", "min_score", "model", "quiet_hours")

TRUE_WORDS = {"1", "true", "yes", "on", "y", "是", "开"}
FALSE_WORDS = {"0", "false", "no", "off", "n", "否", "关"}
KEYWORD_SPLIT = re.compile(r"[,，、;；\s]+")


def parse_bool(value: Any, default: bool | None = None) -> bool | None:
    """宽松地读布尔：认 true/1/是/开 与 false/0/否/关，认不出就用默认值。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    return default


def parse_keywords(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        parts: list[Any] = KEYWORD_SPLIT.split(value)
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        return ()
    words: list[str] = []
    for part in parts:
        word = str(part or "").strip()
        if word and word not in words:
            words.append(word)
    return tuple(words)


def parse_clock(text: Any) -> tuple[int, int] | None:
    """把 `23:00-07:00` 解析成 (起始分钟, 结束分钟)；非法或等长时段返回 None。"""
    raw = str(text or "").strip().replace("～", "-").replace("~", "-")
    if "-" not in raw:
        return None
    start_text, _, end_text = raw.partition("-")
    try:
        start_hour, start_minute = (int(part) for part in start_text.strip().split(":", 1))
        end_hour, end_minute = (int(part) for part in end_text.strip().split(":", 1))
    except (TypeError, ValueError):
        return None
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23):
        return None
    if not (0 <= start_minute <= 59 and 0 <= end_minute <= 59):
        return None
    start, end = start_hour * 60 + start_minute, end_hour * 60 + end_minute
    if start == end:
        return None
    return start, end


def normalize_hours(value: Any) -> str:
    bounds = parse_clock(value)
    if bounds is None:
        return ""
    start, end = bounds
    return f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"


def normalize_entry(value: Mapping[str, Any]) -> dict[str, Any]:
    """只留下认得的字段，并把它们规整成固定类型；认不出的字段直接丢掉。"""
    entry: dict[str, Any] = {}
    if "quiet" in value:
        flag = parse_bool(value.get("quiet"))
        if flag is not None:
            entry["quiet"] = flag
    keywords = parse_keywords(value.get("keywords"))
    if keywords:
        entry["keywords"] = keywords
    if "min_score" in value:
        try:
            score = int(value.get("min_score"))
        except (TypeError, ValueError):
            score = 0
        if score > 0:
            entry["min_score"] = score
    model = str(value.get("model") or "").strip().lower()
    if model in MODEL_CHOICES:
        entry["model"] = model
    hours = normalize_hours(value.get("quiet_hours"))
    if hours:
        entry["quiet_hours"] = hours
    return entry


def parse_policies(raw: Any) -> dict[str, dict[str, Any]]:
    """解析 `QQ_DIGEST_GROUP_POLICIES`：坏 JSON / 坏条目一律跳过，不抛异常。"""
    if isinstance(raw, Mapping):
        data: Any = dict(raw)
    else:
        text = str(raw or "").strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return {}
    if not isinstance(data, Mapping):
        return {}
    policies: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        name = str(key or "").strip()
        if not name or not isinstance(value, Mapping):
            continue
        entry = normalize_entry(value)
        if entry:
            policies[name] = entry
    return policies


@dataclass(frozen=True)
class GroupPolicy:
    """一个群**解析后**的策略：没被覆盖的字段就是全局默认值。"""

    group_id: str = ""
    name: str = ""
    quiet: bool = False
    keywords: tuple[str, ...] = ()
    min_score: int = 0
    model: str = MODEL_DEFAULT
    quiet_hours: str = ""
    matched: str = ""
    overrides: tuple[str, ...] = ()

    @property
    def overridden(self) -> bool:
        return bool(self.overrides)

    @property
    def model_label(self) -> str:
        return MODEL_LABELS.get(self.model, self.model)

    def describe(self) -> str:
        bits = [f"安静群={'是' if self.quiet else '否'}", f"最低分={self.min_score}"]
        if self.keywords:
            bits.append("关键词=" + "、".join(self.keywords[:4]))
        bits.append(f"模型档={self.model_label}")
        if self.quiet_hours:
            bits.append(f"免打扰={self.quiet_hours}")
        return " · ".join(bits)


def lookup(
    policies: Mapping[str, Mapping[str, Any]], group_id: str, name: str
) -> tuple[dict[str, Any], str]:
    """先按群号找，再按群名找；找不到返回空策略。"""
    gid = str(group_id or "").strip()
    label = str(name or "").strip()
    if gid and gid in policies:
        return dict(policies[gid]), gid
    if label and label in policies:
        return dict(policies[label]), label
    return {}, ""


def _legacy_quiet(settings: Any, group_id: str, name: str) -> bool:
    """`QQ_DIGEST_QUIET_GROUPS` 里的群仍然算安静群（没被策略覆盖时）。"""
    listed = tuple(str(item) for item in (getattr(settings, "quiet_groups", ()) or ()))
    if not listed:
        return False
    if str(group_id or "") in listed:
        return True
    label = str(name or "").strip()
    if not label:
        return False
    getter = getattr(settings, "group_name", None)
    for candidate in listed:
        if label == candidate:
            return True
        if callable(getter):
            try:
                alias = str(getter(candidate) or "")
            except Exception:  # noqa: BLE001 - 别名解析失败不该拖垮判定
                alias = ""
            if alias and label == alias:
                return True
    return False


def resolve(group_id: str, name: str, settings: Any) -> GroupPolicy:
    """把「本群策略 + 全局默认」合并成一条可用的策略（默认继承）。"""
    policies = getattr(settings, "group_policies", None) or {}
    entry, matched = lookup(policies, group_id, name)
    overrides: list[str] = []

    quiet = _legacy_quiet(settings, group_id, name)
    if "quiet" in entry:
        quiet = bool(entry["quiet"])
        overrides.append("quiet")

    min_score = int(getattr(settings, "min_score", 0) or 0)
    if "min_score" in entry:
        min_score = int(entry["min_score"])
        overrides.append("min_score")

    model = MODEL_DEFAULT
    if entry.get("model"):
        model = str(entry["model"])
        overrides.append("model")

    quiet_hours = ""
    if entry.get("quiet_hours"):
        quiet_hours = str(entry["quiet_hours"])
        overrides.append("quiet_hours")

    keywords = tuple(entry.get("keywords") or ())
    if keywords:
        overrides.append("keywords")

    return GroupPolicy(
        group_id=str(group_id or ""),
        name=str(name or ""),
        quiet=quiet,
        keywords=keywords,
        min_score=min_score,
        model=model,
        quiet_hours=quiet_hours,
        matched=matched,
        overrides=tuple(overrides),
    )


def in_quiet_hours(policy: GroupPolicy, moment: dt.datetime) -> bool:
    """群免打扰：支持跨零点（22:00-07:00），非法时段一律当作没配。"""
    bounds = parse_clock(policy.quiet_hours)
    if bounds is None:
        return False
    start, end = bounds
    current = moment.hour * 60 + moment.minute
    if start < end:
        return start <= current < end
    return current >= start or current < end


def keyword_hit(policy: GroupPolicy, text: str) -> str:
    """命中的第一个群关键词；没配关键词或没命中就返回空串。"""
    value = str(text or "")
    if not value or not policy.keywords:
        return ""
    for word in policy.keywords:
        if word and word in value:
            return word
    return ""


__all__ = [
    "FIELDS",
    "GroupPolicy",
    "MODEL_CHOICES",
    "MODEL_DEFAULT",
    "MODEL_LABELS",
    "MODEL_LIGHT",
    "MODEL_RULE",
    "MODEL_STRONG",
    "in_quiet_hours",
    "keyword_hit",
    "lookup",
    "normalize_entry",
    "normalize_hours",
    "parse_bool",
    "parse_clock",
    "parse_keywords",
    "parse_policies",
    "resolve",
]
