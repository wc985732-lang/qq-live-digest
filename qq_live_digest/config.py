"""从环境变量 / .env 读取配置。密钥只存本机，不写入日志。"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from . import grouppolicy

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DASHSCOPE_ENDPOINT = (
    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
)

TRUE_VALUES = {"1", "true", "yes", "on", "y", "是"}
FALSE_VALUES = {"0", "false", "no", "off", "n", "否", ""}


def load_env_file(path: Path | str) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，支持 # 注释与引号，不覆盖已存在环境变量。"""
    path = Path(path)
    loaded: dict[str, str] = {}
    if not path.is_file():
        return loaded
    for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value
        if key:
            loaded[key] = value
    return loaded


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return default


def parse_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def split_list(value: Any) -> tuple[str, ...]:
    """逗号/分号/换行分隔的列表。"""
    if value is None or isinstance(value, (list, tuple, set)):
        items = list(value) if not isinstance(value, str) else []
    else:
        text = str(value)
        for separator in (";", "；", "\n", "，", "、"):
            text = text.replace(separator, ",")
        items = text.split(",")
    return tuple(str(item).strip() for item in items if str(item).strip())


def parse_quiet_hours(value: Any) -> str:
    """规范化 23:00-07:00 形式的静默时段；非法或等长时段视为关闭。"""
    text = str(value or "").strip().replace("～", "-").replace("~", "-")
    if not text or "-" not in text:
        return ""
    start_text, _, end_text = text.partition("-")

    def parse_clock(raw: str) -> int | None:
        parts = str(raw or "").strip().split(":")
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            return None
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour * 60 + minute
    start, end = parse_clock(start_text), parse_clock(end_text)
    if start is None or end is None or start == end:
        return ""
    return f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"


def parse_aliases(value: Any) -> dict[str, str]:
    """解析 openid=群名 形式的别名表。"""
    aliases: dict[str, str] = {}
    for chunk in split_list(value):
        key, separator, name = chunk.partition("=")
        if separator:
            key, name = key.strip(), name.strip()
            if key and name:
                aliases[key] = name
    return aliases


@dataclass
class Settings:
    appid: str = ""
    secret: str = ""
    sandbox: bool = False
    official_bot_enabled: bool = True
    group_whitelist: tuple[str, ...] = ()
    group_aliases: Mapping[str, str] = field(default_factory=dict)
    quiet_groups: tuple[str, ...] = ()
    group_policies: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    immediate_groups: tuple[str, ...] = ()
    push_c2c_openids: tuple[str, ...] = ()

    wxpusher_app_token: str = ""
    wxpusher_uids: tuple[str, ...] = ()
    wxpusher_topic_ids: tuple[int, ...] = ()
    serverchan_keys: tuple[str, ...] = ()
    pushplus_tokens: tuple[str, ...] = ()
    webhook_urls: tuple[str, ...] = ()

    # 多端推送扩展（A14）：ntfy / Telegram / Discord / 企业微信 / 邮件。
    # 与已有通道一样参与「主通道失败 → 回退其它通道」的派发，不需要额外开关。
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topics: tuple[str, ...] = ()
    ntfy_token: str = ""
    telegram_bot_token: str = ""
    telegram_chat_ids: tuple[str, ...] = ()
    discord_webhook_urls: tuple[str, ...] = ()
    wecom_webhook_keys: tuple[str, ...] = ()
    email_smtp_host: str = ""
    email_smtp_port: int = 465
    email_smtp_user: str = ""
    email_smtp_password: str = ""
    email_from: str = ""
    email_to: tuple[str, ...] = ()
    email_starttls: bool = True

    dashscope_api_key: str = ""
    dashscope_model: str = "qwen-plus"
    dashscope_endpoint: str = DEFAULT_DASHSCOPE_ENDPOINT
    llm_enabled: bool = True
    llm_provider: str = "openai-compat"
    include_raw: bool = False

    # LLM 失败重试与降级：先原地重试，仍失败则推迟这一批，超过上限才回退本地规则。
    llm_max_retries: int = 2
    llm_retry_backoff: float = 1.5
    llm_defer_max_attempts: int = 3
    llm_defer_window_minutes: int = 15

    # 成本统计（A5）：单价按「元 / 百万 token」计，仅用于把已记录的 token 折算成费用。
    # 只影响展示，不改历史数据；不配置则只统计 token、费用显示为 0。
    llm_price_in: float = 0.0
    llm_price_out: float = 0.0

    # 模型分级路由（A6）：配了轻量模型就按「规则 → 轻量 → 高能力」分流，并把路由原因
    # 记进 llm_calls。留空 = 不启用分级，一切照旧走高能力模型。
    llm_model_light: str = ""
    llm_route_max_light_items: int = 6
    llm_route_easy_local: bool = False

    # 群文件 / 群图片解析
    attachments_enabled: bool = True
    attachment_max_mb: int = 10
    attachment_max_per_hour: int = 30
    attachment_retention_days: int = 7
    vision_enabled: bool = True
    vision_model: str = "qwen3-vl-plus"
    document_max_chars: int = 30000
    pdf_ocr_max_pages: int = 20
    dedupe_hours: int = 6
    # A11 事件级跨群聚合：默认关，避免改变既有推送口径；用 QQ_DIGEST_EVENT_MERGE=1 打开
    event_merge: bool = False
    html_push: bool = True

    # 截止提醒：早上列今天要做的，晚上再提醒一次
    deadline_reminders_enabled: bool = True
    deadline_morning: str = "07:30"
    deadline_evening: str = "21:00"

    # 事务闭环：候选确认卡和每周复盘
    candidate_push_enabled: bool = True
    candidate_min_confidence: float = 0.55
    candidate_reminder_hours: int = 24
    candidate_push_max_per_tick: int = 2
    candidate_push_max_per_day: int = 4
    weekly_review_enabled: bool = True
    # 推送预算与夜间静默：非紧急推送统一限流，紧急事项始终放行。
    push_daily_budget: int = 12
    quiet_hours: str = "23:00-07:00"
    weekly_review_weekday: int = 6
    weekly_review_time: str = "20:30"

    # 手机端待办台（Tailscale 或局域网访问）
    web_enabled: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = 8766
    web_base_url: str = ""
    web_token: str = ""

    window_minutes: int = 10
    poll_seconds: int = 20
    urgent_immediate: bool = True
    min_score: int = 3
    max_items: int = 30
    max_batch: int = 400

    delivery_max_attempts: int = 4
    delivery_retry_seconds: int = 90
    message_retention_days: int = 30

    onebot_enabled: bool = False
    onebot_host: str = "127.0.0.1"
    onebot_port: int = 8765
    onebot_token: str = ""

    # NapCat 历史消息补采：用于服务重启、NapCat 掉线、关机期间的消息找回。
    catchup_enabled: bool = False
    catchup_hours: int = 24
    catchup_count: int = 50
    catchup_interval_minutes: int = 30
    napcat_api_url: str = "http://127.0.0.1:3000"
    napcat_api_token: str = ""

    http_timeout: int = 15
    llm_timeout: int = 60
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")
    log_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "logs")
    env_file: Path | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, env_file: Path | None = None) -> "Settings":
        if env_file is not None:
            load_env_file(env_file)
        elif env is None:
            env_file = Path(os.environ.get("QQ_DIGEST_ENV", PROJECT_ROOT / ".env"))
            load_env_file(env_file)
        source = env if env is not None else os.environ

        def get(name: str, default: str = "") -> str:
            value = source.get(name)
            return default if value is None else str(value)

        data_dir_text = get("QQ_DIGEST_DATA_DIR").strip() or str(PROJECT_ROOT / "data")
        log_dir_text = get("QQ_DIGEST_LOG_DIR").strip() or str(PROJECT_ROOT / "logs")
        data_dir = Path(data_dir_text).expanduser()
        log_dir = Path(log_dir_text).expanduser()
        topic_ids = tuple(
            item for item in (parse_int(value, 0) for value in split_list(get("WXPUSHER_TOPIC_IDS"))) if item
        )
        onebot_token = get("QQ_DIGEST_ONEBOT_TOKEN").strip()
        napcat_api_token = get("QQ_DIGEST_NAPCAT_API_TOKEN").strip() or onebot_token

        return cls(
            appid=get("QQ_BOT_APPID").strip(),
            secret=get("QQ_BOT_SECRET").strip(),
            sandbox=parse_bool(get("QQ_BOT_SANDBOX"), False),
            official_bot_enabled=parse_bool(get("QQ_DIGEST_OFFICIAL_BOT_ENABLED", "1"), True),
            group_whitelist=split_list(get("QQ_DIGEST_GROUPS")),
            group_aliases=parse_aliases(get("QQ_DIGEST_GROUP_ALIASES")),
            quiet_groups=split_list(get("QQ_DIGEST_QUIET_GROUPS")),
            group_policies=grouppolicy.parse_policies(get("QQ_DIGEST_GROUP_POLICIES")),
            immediate_groups=split_list(get("QQ_DIGEST_IMMEDIATE_GROUPS")),
            push_c2c_openids=split_list(get("QQ_DIGEST_PUSH_OPENIDS")),
            wxpusher_app_token=get("WXPUSHER_APP_TOKEN").strip(),
            wxpusher_uids=split_list(get("WXPUSHER_UIDS")),
            wxpusher_topic_ids=topic_ids,
            serverchan_keys=split_list(get("SERVERCHAN_KEYS")),
            pushplus_tokens=split_list(get("PUSHPLUS_TOKENS")),
            webhook_urls=split_list(get("QQ_DIGEST_WEBHOOKS")),
            ntfy_url=get("QQ_DIGEST_NTFY_URL", "https://ntfy.sh").strip() or "https://ntfy.sh",
            ntfy_topics=split_list(get("NTFY_TOPICS")),
            ntfy_token=get("NTFY_TOKEN").strip(),
            telegram_bot_token=get("TELEGRAM_BOT_TOKEN").strip(),
            telegram_chat_ids=split_list(get("TELEGRAM_CHAT_IDS")),
            discord_webhook_urls=split_list(get("QQ_DIGEST_DISCORD_WEBHOOKS")),
            wecom_webhook_keys=split_list(get("QQ_DIGEST_WECOM_KEYS")),
            email_smtp_host=get("QQ_DIGEST_SMTP_HOST").strip(),
            email_smtp_port=max(1, parse_int(get("QQ_DIGEST_SMTP_PORT", "465"), 465)),
            email_smtp_user=get("QQ_DIGEST_SMTP_USER").strip(),
            email_smtp_password=get("QQ_DIGEST_SMTP_PASSWORD").strip(),
            email_from=get("QQ_DIGEST_MAIL_FROM").strip(),
            email_to=split_list(get("QQ_DIGEST_MAIL_TO")),
            email_starttls=parse_bool(get("QQ_DIGEST_SMTP_STARTTLS", "1"), True),
            dashscope_api_key=get("DASHSCOPE_API_KEY").strip(),
            dashscope_model=get("QQ_DIGEST_LLM_MODEL", "qwen-plus").strip() or "qwen-plus",
            dashscope_endpoint=get("QQ_DIGEST_LLM_ENDPOINT", DEFAULT_DASHSCOPE_ENDPOINT).strip()
            or DEFAULT_DASHSCOPE_ENDPOINT,
            llm_enabled=parse_bool(get("QQ_DIGEST_LLM", "1"), True),
            llm_provider=get("QQ_DIGEST_LLM_PROVIDER", "openai-compat").strip().lower()
            or "openai-compat",
            include_raw=parse_bool(get("QQ_DIGEST_INCLUDE_RAW", "0"), False),
            llm_max_retries=max(0, parse_int(get("QQ_DIGEST_LLM_MAX_RETRIES", "2"), 2)),
            llm_retry_backoff=max(
                0.0, parse_float(get("QQ_DIGEST_LLM_RETRY_BACKOFF", "1.5"), 1.5)
            ),
            llm_defer_max_attempts=max(
                0, parse_int(get("QQ_DIGEST_LLM_DEFER_MAX_ATTEMPTS", "3"), 3)
            ),
            llm_defer_window_minutes=max(
                1, parse_int(get("QQ_DIGEST_LLM_DEFER_WINDOW_MINUTES", "15"), 15)
            ),
            llm_price_in=max(0.0, parse_float(get("QQ_DIGEST_LLM_PRICE_IN", "0"), 0.0)),
            llm_price_out=max(0.0, parse_float(get("QQ_DIGEST_LLM_PRICE_OUT", "0"), 0.0)),
            llm_model_light=get("QQ_DIGEST_LLM_MODEL_LIGHT", "").strip(),
            llm_route_max_light_items=max(
                1, parse_int(get("QQ_DIGEST_LLM_ROUTE_MAX_LIGHT_ITEMS", "6"), 6)
            ),
            llm_route_easy_local=parse_bool(get("QQ_DIGEST_LLM_ROUTE_EASY_LOCAL", "0"), False),
            attachments_enabled=parse_bool(get("QQ_DIGEST_ATTACHMENTS", "1"), True),
            attachment_max_mb=max(1, parse_int(get("QQ_DIGEST_ATTACHMENT_MAX_MB", "10"), 10)),
            attachment_max_per_hour=max(
                1, parse_int(get("QQ_DIGEST_ATTACHMENT_MAX_PER_HOUR", "30"), 30)
            ),
            attachment_retention_days=max(
                1, parse_int(get("QQ_DIGEST_ATTACHMENT_RETENTION_DAYS", "7"), 7)
            ),
            vision_enabled=parse_bool(get("QQ_DIGEST_VISION", "1"), True),
            vision_model=get("QQ_DIGEST_VL_MODEL", "qwen3-vl-plus").strip()
            or "qwen3-vl-plus",
            document_max_chars=max(
                1000, parse_int(get("QQ_DIGEST_DOCUMENT_MAX_CHARS", "30000"), 30000)
            ),
            pdf_ocr_max_pages=max(
                1, parse_int(get("QQ_DIGEST_PDF_OCR_MAX_PAGES", "20"), 20)
            ),
            dedupe_hours=max(0, parse_int(get("QQ_DIGEST_DEDUPE_HOURS", "6"), 6)),
            event_merge=parse_bool(get("QQ_DIGEST_EVENT_MERGE", "0"), False),
            html_push=parse_bool(get("QQ_DIGEST_PUSH_HTML", "1"), True),
            deadline_reminders_enabled=parse_bool(
                get("QQ_DIGEST_DEADLINE_REMINDERS", "1"), True
            ),
            deadline_morning=get("QQ_DIGEST_DEADLINE_MORNING", "07:30").strip() or "07:30",
            deadline_evening=get("QQ_DIGEST_DEADLINE_EVENING", "21:00").strip() or "21:00",
            candidate_push_enabled=parse_bool(get("QQ_DIGEST_CANDIDATE_PUSH", "1"), True),
            candidate_min_confidence=min(
                1.0, max(0.0, parse_float(get("QQ_DIGEST_CANDIDATE_MIN_CONFIDENCE", "0.55"), 0.55))
            ),
            candidate_reminder_hours=max(
                6, parse_int(get("QQ_DIGEST_CANDIDATE_REMINDER_HOURS", "24"), 24)
            ),
            candidate_push_max_per_tick=max(
                0, parse_int(get("QQ_DIGEST_CANDIDATE_MAX_PER_TICK", "2"), 2)
            ),
            candidate_push_max_per_day=max(
                0, parse_int(get("QQ_DIGEST_CANDIDATE_MAX_PER_DAY", "4"), 4)
            ),
            weekly_review_enabled=parse_bool(get("QQ_DIGEST_WEEKLY_REVIEW", "1"), True),
            push_daily_budget=max(0, parse_int(get("QQ_DIGEST_PUSH_DAILY_BUDGET", "12"), 12)),
            quiet_hours=parse_quiet_hours(get("QQ_DIGEST_QUIET_HOURS", "23:00-07:00")),
            weekly_review_weekday=min(
                6, max(0, parse_int(get("QQ_DIGEST_WEEKLY_REVIEW_WEEKDAY", "6"), 6))
            ),
            weekly_review_time=get("QQ_DIGEST_WEEKLY_REVIEW_TIME", "20:30").strip() or "20:30",
            web_enabled=parse_bool(get("QQ_DIGEST_WEB", "1"), True),
            web_host=get("QQ_DIGEST_WEB_HOST", "0.0.0.0").strip() or "0.0.0.0",
            web_port=parse_int(get("QQ_DIGEST_WEB_PORT", "8766"), 8766),
            web_base_url=get("QQ_DIGEST_WEB_BASE_URL", "").strip().rstrip("/"),
            web_token=get("QQ_DIGEST_WEB_TOKEN", "").strip(),
            window_minutes=max(1, parse_int(get("QQ_DIGEST_WINDOW_MINUTES", "10"), 10)),
            poll_seconds=max(5, parse_int(get("QQ_DIGEST_POLL_SECONDS", "20"), 20)),
            urgent_immediate=parse_bool(get("QQ_DIGEST_URGENT_IMMEDIATE", "1"), True),
            min_score=parse_int(get("QQ_DIGEST_MIN_SCORE", "3"), 3),
            max_items=max(1, parse_int(get("QQ_DIGEST_MAX_ITEMS", "30"), 30)),
            max_batch=max(50, parse_int(get("QQ_DIGEST_MAX_BATCH", "400"), 400)),
            delivery_max_attempts=max(1, parse_int(get("QQ_DIGEST_MAX_ATTEMPTS", "4"), 4)),
            delivery_retry_seconds=max(10, parse_int(get("QQ_DIGEST_RETRY_SECONDS", "90"), 90)),
            message_retention_days=max(1, parse_int(get("QQ_DIGEST_RETENTION_DAYS", "30"), 30)),
            onebot_enabled=parse_bool(get("QQ_DIGEST_ONEBOT_ENABLED", "0"), False),
            onebot_host=get("QQ_DIGEST_ONEBOT_HOST", "127.0.0.1").strip() or "127.0.0.1",
            onebot_port=parse_int(get("QQ_DIGEST_ONEBOT_PORT", "8765"), 8765),
            onebot_token=onebot_token,
            catchup_enabled=parse_bool(get("QQ_DIGEST_CATCHUP_ENABLED", "0"), False),
            catchup_hours=max(1, parse_int(get("QQ_DIGEST_CATCHUP_HOURS", "24"), 24)),
            catchup_count=max(1, parse_int(get("QQ_DIGEST_CATCHUP_COUNT", "50"), 50)),
            catchup_interval_minutes=max(5, parse_int(get("QQ_DIGEST_CATCHUP_INTERVAL_MINUTES", "30"), 30)),
            napcat_api_url=get("QQ_DIGEST_NAPCAT_API_URL", "http://127.0.0.1:3000").strip()
            or "http://127.0.0.1:3000",
            napcat_api_token=napcat_api_token,
            http_timeout=max(5, parse_int(get("QQ_DIGEST_HTTP_TIMEOUT", "15"), 15)),
            llm_timeout=max(10, parse_int(get("QQ_DIGEST_LLM_TIMEOUT", "60"), 60)),
            data_dir=data_dir,
            log_dir=log_dir,
            env_file=env_file,
        )

    def group_name(self, group_id: str) -> str:
        return self.group_aliases.get(group_id) or group_id or "未知群"

    def accepts_group(self, group_id: str) -> bool:
        # fail-closed：未配置白名单时不处理任何群，避免照抄模板后默认接收全部群。
        return bool(self.group_whitelist) and group_id in self.group_whitelist

    def is_immediate_group(self, group_id: str) -> bool:
        return str(group_id or "") in self.immediate_groups

    def group_policy(self, group_id: str, name: str = "") -> grouppolicy.GroupPolicy:
        """本群解析后的策略：没写的字段继承全局（A9）。"""
        return grouppolicy.resolve(str(group_id or ""), str(name or ""), self)

    def is_quiet_group(self, group_id: str) -> bool:
        return self.group_policy(group_id).quiet

    def is_quiet_group_name(self, name: str) -> bool:
        target = str(name or "").strip()
        if not target:
            return False
        return self.group_policy("", target).quiet

    def quiet_hours_range(self) -> tuple[int, int] | None:
        """返回 (起始分钟, 结束分钟)；未配置或非法时返回 None。"""
        text = str(self.quiet_hours or "").strip()
        if "-" not in text:
            return None
        start_text, _, end_text = text.partition("-")
        try:
            start_hour, start_minute = (int(part) for part in start_text.split(":", 1))
            end_hour, end_minute = (int(part) for part in end_text.split(":", 1))
        except (TypeError, ValueError):
            return None
        if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23):
            return None
        start, end = start_hour * 60 + start_minute, end_hour * 60 + end_minute
        if start == end:
            return None
        return start, end

    def in_quiet_hours(self, moment: dt.datetime) -> bool:
        bounds = self.quiet_hours_range()
        if bounds is None:
            return False
        start, end = bounds
        current = moment.hour * 60 + moment.minute
        if start < end:
            return start <= current < end
        # 跨零点，例如 23:00-07:00
        return current >= start or current < end

    def push_channels(self) -> list[str]:
        channels: list[str] = []
        if self.official_bot_enabled and self.push_c2c_openids and self.appid and self.secret:
            channels.append("qq-bot")
        if self.wxpusher_app_token and (self.wxpusher_uids or self.wxpusher_topic_ids):
            channels.append("wxpusher")
        if self.serverchan_keys:
            channels.append("serverchan")
        if self.pushplus_tokens:
            channels.append("pushplus")
        if self.webhook_urls:
            channels.append("webhook")
        if self.ntfy_topics:
            channels.append("ntfy")
        if self.telegram_bot_token and self.telegram_chat_ids:
            channels.append("telegram")
        if self.discord_webhook_urls:
            channels.append("discord")
        if self.wecom_webhook_keys:
            channels.append("wecom")
        if self.email_smtp_host and self.email_to:
            channels.append("email")
        return channels

    def problems(self) -> list[str]:
        """返回阻塞启动的问题列表（空列表表示可启动）。"""
        issues: list[str] = []
        if self.official_bot_enabled and (not self.appid or not self.secret):
            issues.append("缺少 QQ_BOT_APPID / QQ_BOT_SECRET，官方机器人无法登录。")
        if not self.push_channels():
            issues.append(
                "未配置任何推送通道（QQ 私聊 openid / WxPusher / Server酱 / PushPlus / Webhook / "
                "ntfy / Telegram / Discord / 企业微信 / 邮件）。"
            )
        if not self.group_whitelist:
            issues.append("QQ_DIGEST_GROUPS 未配置：不会处理任何群；请在 .env 填写要监控的群号。")
        if self.onebot_enabled and not self.onebot_token:
            issues.append("已启用 OneBot 接收但未设置 QQ_DIGEST_ONEBOT_TOKEN，接口无鉴权。")
        if self.catchup_enabled and not self.napcat_api_token:
            issues.append("已启用历史补采但未设置 NapCat API Token，补采会失败。")
        return issues

    def describe(self) -> dict[str, Any]:
        """用于日志/doctor 的脱敏描述。"""
        return {
            "appid": _mask(self.appid),
            "official_bot_enabled": self.official_bot_enabled,
            "sandbox": self.sandbox,
            # 见 accepts_group：空白名单是 fail-closed，不是“全部群”。
            "groups": list(self.group_whitelist) or ["<未配置：不处理任何群>"],
            "group_aliases": dict(self.group_aliases),
            "quiet_groups": list(self.quiet_groups),
            "group_policies": {
                str(key): {
                    str(field_name): (list(value) if isinstance(value, tuple) else value)
                    for field_name, value in dict(entry).items()
                }
                for key, entry in self.group_policies.items()
            },
            "immediate_groups": list(self.immediate_groups),
            "push_channels": self.push_channels(),
            "push_openids": [_mask(item) for item in self.push_c2c_openids],
            "llm": bool(self.llm_enabled and self.dashscope_api_key),
            "llm_model": self.dashscope_model,
            "llm_provider": self.llm_provider,
            "llm_timeout": self.llm_timeout,
            "llm_retries": self.llm_max_retries,
            "llm_defer_max_attempts": self.llm_defer_max_attempts,
            "llm_price_in": self.llm_price_in,
            "llm_price_out": self.llm_price_out,
            "llm_model_light": self.llm_model_light,
            "include_raw": self.include_raw,
            "attachments": {
                "enabled": bool(self.attachments_enabled and self.dashscope_api_key),
                "max_mb": self.attachment_max_mb,
                "max_per_hour": self.attachment_max_per_hour,
                "retention_days": self.attachment_retention_days,
                "vision": bool(self.vision_enabled and self.dashscope_api_key),
                "vision_model": self.vision_model,
                "document_max_chars": self.document_max_chars,
                "pdf_ocr_max_pages": self.pdf_ocr_max_pages,
            },
            "dedupe_hours": self.dedupe_hours,
            "event_merge": self.event_merge,
            "html_push": self.html_push,
            "deadline_reminders": {
                "enabled": self.deadline_reminders_enabled,
                "morning": self.deadline_morning,
                "evening": self.deadline_evening,
            },
            "web": {
                "enabled": bool(self.web_enabled),
                "host": self.web_host,
                "port": self.web_port,
                "base_url": self.web_base_url,
                "auth": bool(self.web_token),
            },
            "transaction_loop": {
                "candidate_push": bool(self.candidate_push_enabled),
                "candidate_min_confidence": self.candidate_min_confidence,
                "candidate_reminder_hours": self.candidate_reminder_hours,
                "candidate_push_max_per_day": self.candidate_push_max_per_day,
                "weekly_review": bool(self.weekly_review_enabled),
                "weekly_review_weekday": self.weekly_review_weekday,
                "weekly_review_time": self.weekly_review_time,
            },
            "window_minutes": self.window_minutes,
            "push_budget": {
                "daily": self.push_daily_budget,
                "quiet_hours": self.quiet_hours,
            },
            "urgent_immediate": self.urgent_immediate,
            "min_score": self.min_score,
            "onebot": {"enabled": self.onebot_enabled, "host": self.onebot_host, "port": self.onebot_port},
            "catchup": {
                "enabled": self.catchup_enabled,
                "hours": self.catchup_hours,
                "count": self.catchup_count,
                "interval_minutes": self.catchup_interval_minutes,
                "api_url": self.napcat_api_url,
            },
            "data_dir": str(self.data_dir),
            "log_dir": str(self.log_dir),
        }


def _mask(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-2:]}"


def ensure_dirs(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
