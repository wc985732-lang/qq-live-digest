"""全链路自检（Roadmap A19）。

设计约束：

- 所有外部探测都通过可注入的 client / 函数完成，测试全程离线，不依赖 NapCat、网络或真实群；
- 每条检查输出 OK / WARN / FAIL 与一句可执行的修复建议；
- 输出脱敏：不回显 token、不打印 .env 全文，群号只显示数量与掩码。
"""

from __future__ import annotations

import dataclasses
import json
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable, Sequence

from . import providers
from . import llmstats
from . import confidence
from .bot import MISSING_BOTPY_HINT, botpy_available, botpy_version
from .catchup import NapCatClient, NapCatError
from .config import Settings
from .store import Store

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"

_SEVERITY = {OK: 0, WARN: 1, FAIL: 2}
_LOOPBACK = {"127.0.0.1", "localhost", "::1", ""}


@dataclasses.dataclass(frozen=True)
class Check:
    """一条自检结果。hint 是一句「接下来做什么」，没有问题时留空。"""

    name: str
    status: str
    detail: str = ""
    hint: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "hint": self.hint}


def _mask_id(value: Any) -> str:
    """群号/账号脱敏：只保留首尾各 2 位。"""
    text = str(value or "")
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}***{text[-2:]}"


def _is_loopback(host: str) -> bool:
    return str(host or "").strip().lower() in _LOOPBACK


def _health_host(host: str) -> str:
    """监听地址不能直接当请求地址：0.0.0.0 / :: 换成本机回环。"""
    text = str(host or "").strip()
    return "127.0.0.1" if text in {"0.0.0.0", "::", "[::]", ""} else text


def _default_http_get(url: str, timeout: int) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return int(error.code), error.read().decode("utf-8", errors="replace")[:200]
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise OSError(str(error)) from error


@dataclasses.dataclass
class DoctorContext:
    """自检上下文；测试通过替换这里的注入口来保持离线。"""

    settings: Settings
    http_get: Callable[[str, int], tuple[int, str]] = _default_http_get
    napcat_factory: Callable[..., Any] = NapCatClient
    open_store: Callable[[], Any] | None = None
    python_version: Sequence[int] = dataclasses.field(default_factory=lambda: tuple(sys.version_info[:3]))
    bot_credential_check: Callable[[], tuple[bool, str]] | None = None

    def store(self) -> Any:
        if self.open_store is not None:
            return self.open_store()
        return Store(self.settings.data_dir / "digest.sqlite3")

    def web_token(self) -> str:
        value = str(self.settings.web_token or "").strip()
        if value:
            return value
        try:
            return str(self.store().meta_get("web_token", "") or "").strip()
        except Exception:  # noqa: BLE001 - 读不到就当没配
            return ""


# ----------------------------------------------------------------- 各项检查
def check_python(ctx: DoctorContext) -> Check:
    version = tuple(int(part) for part in ctx.python_version[:3])
    text = ".".join(str(part) for part in version)
    if version[:2] >= (3, 12):
        return Check("Python 版本", OK, text)
    return Check("Python 版本", WARN, f"{text}（要求 3.12+）", "安装 Python 3.12+ 后重建 .venv")


def check_env_file(ctx: DoctorContext) -> Check:
    path = ctx.settings.env_file
    if path and path.exists():
        return Check("配置文件", OK, f"已加载 {path.name}")
    return Check("配置文件", WARN, "未找到 .env，正在使用默认值", "复制 .env.example 为 .env 并填写群号与推送通道")


def check_groups(ctx: DoctorContext) -> Check:
    groups = [str(item) for item in ctx.settings.group_whitelist if str(item or "").strip()]
    if not groups:
        return Check(
            "群白名单",
            FAIL,
            "未配置任何群：不会处理任何消息",
            "在 .env 的 QQ_DIGEST_GROUPS 里填入要监控的群号（逗号分隔）",
        )
    shown = "、".join(_mask_id(item) for item in groups[:3])
    if len(groups) > 3:
        shown += f" 等 {len(groups)} 个"
    detail = f"{len(groups)} 个群：{shown}（已脱敏）"
    policies = dict(ctx.settings.group_policies or {})
    if policies:
        detail += f" · 其中 {len(policies)} 个配了群策略"
        return Check("群白名单", OK, detail, "看每群生效的开关：python main.py groups")
    return Check("群白名单", OK, detail, "想让某个群安静 / 加关键词 / 单独设最低分：python main.py groups")


def check_push(ctx: DoctorContext) -> Check:
    channels = list(ctx.settings.push_channels())
    if not channels:
        return Check(
            "推送通道",
            FAIL,
            "未配置任何可用通道",
            "至少配置 WxPusher / Server酱 / PushPlus / Webhook 之一",
        )
    return Check("推送通道", OK, "、".join(channels))


def check_onebot(ctx: DoctorContext) -> Check:
    settings = ctx.settings
    where = f"{settings.onebot_host}:{settings.onebot_port}"
    if not settings.onebot_enabled:
        return Check("OneBot 接收器", OK, "未启用（仅补采模式）")
    if str(settings.onebot_token or "").strip():
        return Check("OneBot 接收器", OK, f"监听 {where}，已设置上报 token")
    return Check(
        "OneBot 接收器",
        WARN,
        f"监听 {where}，未设置上报 token",
        "设置 QQ_DIGEST_ONEBOT_TOKEN，否则本机任意进程都能伪造群消息",
    )


def check_llm(ctx: DoctorContext) -> Check:
    settings = ctx.settings
    name = providers.provider_name(settings)
    if name != providers.NULL_PROVIDER and name not in providers.provider_names():
        known = "、".join(providers.provider_names()) or "（无）"
        return Check(
            "大模型",
            FAIL,
            f"未知的模型 Provider：{name}",
            f"QQ_DIGEST_LLM_PROVIDER 只能填 {known}，或填 none 关闭模型调用",
        )
    if str(settings.dashscope_api_key or "").strip():
        extras = []
        if settings.attachments_enabled and settings.vision_enabled:
            extras.append(f"图片识别 {settings.vision_model}")
        suffix = f"，{'；'.join(extras)}" if extras else ""
        return Check(
            "大模型",
            OK,
            f"Provider {name} · 模型 {settings.dashscope_model}{suffix}（key 已配置）",
        )
    if settings.llm_enabled or settings.attachments_enabled:
        return Check(
            "大模型",
            WARN,
            "未配置 DASHSCOPE_API_KEY，摘要/附件将回退本地规则",
            "如需 AI 摘要与图片识别，在 .env 填入 DASHSCOPE_API_KEY",
        )
    return Check("大模型", OK, "未启用，使用本地规则")


def check_storage(ctx: DoctorContext) -> Check:
    try:
        counts = dict(ctx.store().counts())
    except Exception as error:  # noqa: BLE001 - doctor 不能因为打不开库就崩
        return Check("本地存储", FAIL, f"无法打开数据库：{error}", "检查 data/ 目录权限与磁盘剩余空间")
    summary = "、".join(f"{key}={value}" for key, value in sorted(counts.items()))
    return Check("本地存储", OK, summary or "空库")


def check_llm_usage(ctx: DoctorContext) -> Check:
    """最近 24 小时的模型用量与成本：一眼看出「有没有调用、花了多少、失败几次」（A5）。"""
    settings = ctx.settings
    try:
        summary = ctx.store().llm_call_summary(hours=24)
    except Exception as error:  # noqa: BLE001 - doctor 不能因为统计失败就崩
        return Check("模型用量", WARN, f"无法统计：{error}", "不影响摘要推送；稍后重试")
    calls = int(summary.get("calls") or 0)
    if not calls:
        return Check("模型用量", OK, "最近 24 小时没有模型调用（未启用 / 未配密钥 / 没轮到需要 AI 的消息）")
    prompt = int(summary.get("prompt_tokens") or 0)
    completion = int(summary.get("completion_tokens") or 0)
    cost = llmstats.call_cost(
        prompt, completion, price_in=settings.llm_price_in, price_out=settings.llm_price_out
    )
    detail = (
        f"24h 调用 {calls} 次 · 输入 {llmstats.format_tokens(prompt)}"
        f" / 输出 {llmstats.format_tokens(completion)} token · 费用 {llmstats.format_cost(cost)}"
    )
    failed = int(summary.get("failed") or 0)
    retried = int(summary.get("retried") or 0)
    skipped = int(summary.get("skipped") or 0)
    extras = []
    if failed:
        extras.append(f"失败 {failed}")
    if retried:
        extras.append(f"重试 {retried}")
    if skipped:
        extras.append(f"跳过 {skipped}")
    if extras:
        detail += " · " + "、".join(extras)
    if failed:
        return Check("模型用量", WARN, detail, "看明细：python main.py llm-stats --recent 20（失败会自动回退本地规则）")
    if not (settings.llm_price_in or settings.llm_price_out):
        return Check("模型用量", OK, detail, "想换算成费用请配置 QQ_DIGEST_LLM_PRICE_IN / _OUT（元/百万 token）")
    return Check("模型用量", OK, detail)


def check_confidence(ctx: DoctorContext) -> Check:
    """候选置信度分布：每条候选都带把握度与判定依据（A7）。"""
    threshold = float(ctx.settings.candidate_min_confidence or confidence.DEFAULT_LOW)
    try:
        tasks = ctx.store().list_tasks(statuses=("candidate",), limit=200)
    except Exception as error:  # noqa: BLE001 - 统计失败不该拖垮 doctor
        return Check("候选置信度", WARN, f"无法统计：{error}", "不影响摘要推送；稍后重试")
    total = len(tasks)
    if not total:
        return Check("候选置信度", OK, "当前没有待确认候选（每张候选卡片都会带把握度与判定依据）")
    low = sum(1 for task in tasks if float(task.get("confidence") or 0.0) < threshold)
    high = sum(1 for task in tasks if float(task.get("confidence") or 0.0) >= confidence.DEFAULT_HIGH)
    detail = f"待确认 {total} 条 · 把握较高 {high} · 建议人工确认 {low}"
    hint = "低置信度不会自动进待办：在待办台确认或忽略（python main.py web）" if low else ""
    return Check("候选置信度", OK, detail, hint)


def check_feedback(ctx: DoctorContext) -> Check:
    """人工反馈回收：候选确认 / 忽略 / 纠错的闭环（A8）。"""
    try:
        summary = ctx.store().feedback_summary(days=30)
    except Exception as error:  # noqa: BLE001 - 统计失败不该拖垮 doctor
        return Check("反馈闭环", WARN, f"无法统计：{error}", "不影响摘要推送；稍后重试")
    candidates = int(summary.get("candidates") or 0)
    if not candidates:
        return Check("反馈闭环", OK, "最近 30 天没有待确认候选（没有反馈要回收）")
    confirmed = int(summary.get("confirmed") or 0)
    dismissed = int(summary.get("dismissed") or 0)
    corrected = int(summary.get("corrected") or 0)
    detail = (
        f"30 天候选 {candidates} · 确认 {confirmed}"
        f"（{float(summary.get('confirmation_rate') or 0.0):g}%）"
        f" · 忽略 {dismissed}"
        f"（{float(summary.get('dismissal_rate') or 0.0):g}%）"
        f" · 纠错 {corrected}"
    )
    insights = summary.get("insights") or []
    hint = "看明细与规则建议：python main.py feedback"
    if insights:
        hint = f"有 {len(insights)} 条纠错提示：python main.py feedback"
    return Check("反馈闭环", OK, detail, hint)


def check_napcat(ctx: DoctorContext) -> Check:
    settings = ctx.settings
    try:
        client = ctx.napcat_factory(settings.napcat_api_url, settings.napcat_api_token, settings.http_timeout)
        status = client.call("get_status") or {}
    except NapCatError as error:
        message = str(error)
        lowered = message.lower()
        if any(token in lowered for token in ("http 401", "http 403", "token", "unauthorized")):
            return Check("NapCat", FAIL, message, "核对 QQ_DIGEST_NAPCAT_API_TOKEN 与 NapCat 侧配置一致")
        return Check(
            "NapCat",
            WARN,
            f"未连通：{message}",
            "确认 NapCat 与 QQ 已启动并登录（实时接收与历史补采都依赖它）",
        )
    except Exception as error:  # noqa: BLE001 - 探测失败不该让 doctor 崩
        return Check("NapCat", WARN, f"未连通：{error}", "确认 NapCat 与 QQ 已启动并登录")

    data = status.get("data") if isinstance(status.get("data"), dict) else {}
    online = bool(data.get("online"))
    good = bool(data.get("good"))
    if not (online and good):
        return Check(
            "NapCat",
            WARN,
            f"online={online} good={good}",
            "在手机 QQ 上确认登录状态，必要时重启 NapCat",
        )
    detail = "online/good"
    try:
        login = client.login_info() or {}
    except Exception:  # noqa: BLE001 - 登录信息只是附加信息
        login = {}
    if login.get("user_id"):
        detail += f"，登录账号 {_mask_id(login['user_id'])}"
    return Check("NapCat", OK, detail)


def check_receiver(ctx: DoctorContext) -> Check:
    settings = ctx.settings
    host = _health_host(settings.onebot_host)
    url = f"http://{host}:{settings.onebot_port}/health"
    try:
        status, text = ctx.http_get(url, min(5, int(settings.http_timeout or 5)))
    except OSError as error:
        return Check(
            "接收服务",
            WARN,
            f"{settings.onebot_host}:{settings.onebot_port} 未响应（{error}）",
            "服务没在跑就启动：python main.py run",
        )
    try:
        payload = json.loads(text or "{}")
    except json.JSONDecodeError:
        return Check("接收服务", WARN, f"返回不是 JSON：{text[:80]}", "重启服务后再试")
    if status == 200 and payload.get("service") == "qq-live-digest":
        return Check("接收服务", OK, f"{settings.onebot_host}:{settings.onebot_port} 正常")
    return Check("接收服务", WARN, f"HTTP {status}", "重启服务：python main.py run")


def check_web(ctx: DoctorContext) -> Check:
    settings = ctx.settings
    token = ctx.web_token()
    host = str(settings.web_host or "")
    exposed = not _is_loopback(host)
    if exposed and not token:
        return Check(
            "待办台",
            FAIL,
            f"监听 {host}:{settings.web_port} 且未设置访问 token",
            "设置 QQ_DIGEST_WEB_TOKEN（或把 QQ_DIGEST_WEB_HOST 改回 127.0.0.1）",
        )
    url = f"http://{_health_host(host)}:{settings.web_port}/api/health"
    try:
        status, _ = ctx.http_get(url, min(5, int(settings.http_timeout or 5)))
    except OSError:
        return Check("待办台", WARN, f"{host}:{settings.web_port} 未响应", "服务随 main.py run 一起启动")
    if status != 200:
        return Check("待办台", WARN, f"HTTP {status}", "重启服务后再试")
    note = f"（{host} + token 鉴权，仅应在私有网络访问）" if exposed else "（仅本机）"
    return Check("待办台", OK, f"{host}:{settings.web_port}{note}")


def check_access(ctx: DoctorContext) -> Check:
    """访问层：如实列出每个监听地址，并标出对外暴露与鉴权状态。"""
    settings = ctx.settings
    token = ctx.web_token()
    onebot_exposed = not _is_loopback(settings.onebot_host)
    web_exposed = not _is_loopback(settings.web_host)

    parts = [
        f"OneBot {settings.onebot_host}:{settings.onebot_port}"
        + ("（对外监听，建议改回 127.0.0.1）" if onebot_exposed else "（仅本机）"),
        f"待办台 {settings.web_host}:{settings.web_port}"
        + (
            ("（对外监听，" + ("有 token）" if token else "无 token！）"))
            if web_exposed
            else "（仅本机）"
        ),
    ]
    detail = "；".join(parts)

    problems: list[str] = []
    if onebot_exposed:
        problems.append("OneBot 不应对外监听")
    if web_exposed and not token:
        problems.append("待办台对外且无 token")
    if onebot_exposed or web_exposed:
        hint = "对外监听只应在私有网络（如 Tailscale）内访问，不要直接映射公网端口"
    else:
        hint = ""
    return Check("访问层", WARN if problems else OK, detail, hint)


def check_bot_credentials(ctx: DoctorContext) -> Check:
    settings = ctx.settings
    wants_bot = bool(settings.official_bot_enabled) and bool(settings.appid) and bool(settings.secret)
    if wants_bot and not botpy_available():
        # 配好了 AppID/Secret 却导不进 qq-botpy 时，官方机器人在运行期只会静默失效，必须显式报错。
        return Check(
            "QQ 官方机器人",
            FAIL,
            "已配置 AppID/Secret，但当前环境无法导入 qq-botpy",
            MISSING_BOTPY_HINT,
        )
    if ctx.bot_credential_check is None:
        detail = "未做在线校验（加 --online 开启）"
        if wants_bot:
            detail = f"qq-botpy {botpy_version()} 可导入；未做在线校验（加 --online 开启）"
        return Check("QQ 官方机器人", OK, detail)
    passed, message = ctx.bot_credential_check()
    if passed:
        return Check("QQ 官方机器人", OK, message)
    return Check("QQ 官方机器人", FAIL, message, "检查 QQ_BOT_APPID / QQ_BOT_SECRET")


def run_checks(ctx: DoctorContext) -> list[Check]:
    return [
        check_python(ctx),
        check_env_file(ctx),
        check_groups(ctx),
        check_push(ctx),
        check_onebot(ctx),
        check_llm(ctx),
        check_llm_usage(ctx),
        check_confidence(ctx),
        check_feedback(ctx),
        check_storage(ctx),
        check_napcat(ctx),
        check_receiver(ctx),
        check_web(ctx),
        check_access(ctx),
        check_bot_credentials(ctx),
    ]


def worst_status(checks: Iterable[Check]) -> str:
    worst = OK
    for check in checks:
        if _SEVERITY.get(check.status, 0) > _SEVERITY[worst]:
            worst = check.status
    return worst


def as_dicts(checks: Iterable[Check]) -> list[dict[str, str]]:
    return [check.as_dict() for check in checks]


def render(checks: Sequence[Check]) -> str:
    """终端渲染：对齐的状态列 + 建议行。"""
    width = max((len(check.name) for check in checks), default=0)
    lines: list[str] = []
    for check in checks:
        lines.append(f"[{check.status:<4}] {check.name.ljust(width)}  {check.detail}")
        if check.hint:
            lines.append(f"       {' ' * width}  → {check.hint}")
    counts = {status: sum(1 for item in checks if item.status == status) for status in (OK, WARN, FAIL)}
    lines.append("")
    lines.append(f"结论：{counts[OK]} 项正常，{counts[WARN]} 项提醒，{counts[FAIL]} 项需处理")
    return "\n".join(lines)
