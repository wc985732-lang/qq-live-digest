"""模型分级路由（Roadmap A6）。

把「一眼就能看懂」的批次交给便宜的轻量模型，只在难例上动用高能力模型；本地规则能直接
搞定的批次干脆不调模型。**每次决定连同原因**一起记进用量表（`llm_calls.route` /
`llm_calls.route_reason`），所以「这次为什么走了贵的模型」在 `main.py llm-stats` 里查得到。

本模块只做判断：不造 Provider、不发请求、不落库。调用方拿 `RouteDecision.model`
去 `providers.build_provider(settings, model=...)`。

三档的取舍是刻意的保守：

1. 没配轻量模型（`QQ_DIGEST_LLM_MODEL_LIGHT`）时**一切照旧**走高能力模型，行为和 A6 之前一致；
2. 「本地规则直接交付」这一档要显式打开 `QQ_DIGEST_LLM_ROUTE_EASY_LOCAL=1`，
   免得不声不响地少给模型干活；
3. 难例判据只看确定性信号（条数、分值是否贴着阈值、A7 置信度是否判低），不做概率猜测。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from . import confidence as confidence_mod
from . import grouppolicy
from . import llmstats

#: 判定为「难例」前，最多几条候选还给轻量模型处理
DEFAULT_MAX_LIGHT_ITEMS = 6
#: 本地规则交付那一档的条数上限
MAX_LOCAL_ITEMS = 2


@dataclass(frozen=True)
class RouteDecision:
    """一次路由决定：走哪一档、用哪个模型、为什么。"""

    tier: str = llmstats.ROUTE_STRONG
    reason: str = ""
    model: str = ""

    @property
    def label(self) -> str:
        return llmstats.route_label(self.tier)


def _strong_model(settings: Any) -> str:
    return str(getattr(settings, "dashscope_model", "") or "").strip()


def _light_model(settings: Any) -> str:
    return str(getattr(settings, "llm_model_light", "") or "").strip()


def _max_light_items(settings: Any) -> int:
    raw = getattr(settings, "llm_route_max_light_items", DEFAULT_MAX_LIGHT_ITEMS)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_LIGHT_ITEMS


def _notice_level(item: Mapping[str, Any], settings: Any) -> str:
    """候选目前（还没经过 AI 精炼）的置信度档位——复用 A7 的纯函数。"""
    try:
        return str(confidence_mod.assess_notice(item, settings).level)
    except Exception:  # noqa: BLE001 - 路由判断出错不拖累主流程
        return ""


def _hardness(items: list[Mapping[str, Any]], settings: Any) -> str:
    """难例判据；返回人话原因，空串表示「不难」。"""
    limit = _max_light_items(settings)
    if len(items) > limit:
        return f"候选 {len(items)} 条，超过轻量模型上限 {limit} 条"
    min_score = int(getattr(settings, "min_score", 0) or 0)
    for item in items:
        try:
            score = int(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        if min_score and score <= min_score + 1:
            return "有候选分值贴着入摘要阈值，判断容易出错"
        if _notice_level(item, settings) == confidence_mod.LEVEL_LOW:
            return "有低置信度候选，交给更强的模型判断"
    return ""


def _local_is_enough(items: list[Mapping[str, Any]], settings: Any) -> bool:
    """本地规则够不够：条数很少，且每条都已是高置信度。"""
    if not items or len(items) > MAX_LOCAL_ITEMS:
        return False
    return all(
        _notice_level(item, settings) == confidence_mod.LEVEL_HIGH for item in items
    )


def _group_of(items: list[Mapping[str, Any]]) -> tuple[str, str]:
    """整批候选是否来自同一个群；是就返回 (群号, 群名)，否则返回空。"""
    ids = {
        str(item.get("group_id") or ((item.get("record") or {}) if isinstance(item.get("record"), dict) else {}).get("group_id") or "")
        for item in items
    }
    if len(ids) != 1:
        return "", ""
    group_id = ids.pop()
    name = ""
    for item in items:
        message = item.get("message")
        name = str(item.get("group_name") or getattr(message, "source", "") or "")
        if name:
            break
    return group_id, name


def _pinned_decision(
    items: list[Mapping[str, Any]], settings: Any, *, strong: str, light: str
) -> RouteDecision | None:
    """本群策略写死了模型档时优先照办（A9）；混群或没写就交回默认判据。"""
    group_id, name = _group_of(items)
    if not group_id and not name:
        return None
    try:
        policy = settings.group_policy(group_id, name)
    except Exception:  # noqa: BLE001 - 策略解析出错不该影响主流程
        return None
    if not policy.overridden or policy.model == grouppolicy.MODEL_DEFAULT:
        return None
    label = f"本群策略指定走{policy.model_label}"
    if policy.model == grouppolicy.MODEL_RULE:
        return RouteDecision(llmstats.ROUTE_RULE, label, "")
    if policy.model == grouppolicy.MODEL_LIGHT:
        if not light:
            return None  # 本群要轻量模型但没配，交回默认判据（会走高能力模型）
        return RouteDecision(llmstats.ROUTE_LIGHT, label, light)
    if policy.model == grouppolicy.MODEL_STRONG:
        return RouteDecision(llmstats.ROUTE_STRONG, label, strong)
    return None


def decide_route(
    items: Iterable[Mapping[str, Any]],
    settings: Any,
) -> RouteDecision:
    """决定这一批候选走哪一档（规则 / 轻量 / 高能力），并给出原因。"""
    materialised = list(items)
    strong = _strong_model(settings)
    light = _light_model(settings)
    pinned = _pinned_decision(materialised, settings, strong=strong, light=light)
    if pinned is not None:
        return pinned
    if not light:
        return RouteDecision(
            llmstats.ROUTE_STRONG,
            "未配置轻量模型（QQ_DIGEST_LLM_MODEL_LIGHT），走默认模型",
            strong,
        )
    hard = _hardness(materialised, settings)
    if hard:
        return RouteDecision(llmstats.ROUTE_STRONG, hard, strong)
    if bool(getattr(settings, "llm_route_easy_local", False)) and _local_is_enough(materialised, settings):
        return RouteDecision(
            llmstats.ROUTE_RULE,
            f"候选 {len(materialised)} 条且都已高置信，本地规则足够",
            "",
        )
    return RouteDecision(
        llmstats.ROUTE_LIGHT,
        f"候选 {len(materialised)} 条且都够清晰，先用轻量模型",
        light,
    )


__all__ = [
    "DEFAULT_MAX_LIGHT_ITEMS",
    "MAX_LOCAL_ITEMS",
    "RouteDecision",
    "decide_route",
]
