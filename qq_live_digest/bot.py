"""官方 QQ 机器人常驻客户端（qq-botpy 1.2.1 + GROUP_MESSAGE_CREATE 补丁）。

qq-botpy 属于**可选依赖**：没有它时，OneBot 接收、摘要、推送、待办台、doctor 等能力必须照常可用。
因此本模块把 botpy 的导入以及依赖它的类定义全部延迟到真正启动官方机器人时（`_load_botpy()`），
缺失时抛 `BotpyNotInstalled` 交由调用方降级处理，而不是在 import 阶段 `SystemExit` 掉整个进程。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .timeutil import iso, now_local

LOGGER = logging.getLogger(__name__)

RecordCallback = Callable[[dict[str, Any]], Any]
OpenIdCallback = Callable[[dict[str, Any]], Any]

MISSING_BOTPY_HINT = "缺少 qq-botpy，请先执行: pip install -r requirements.txt"
_LAZY_NAMES = ("botpy", "ConnectionState", "GroupMessage", "C2CMessage", "LiveBotClient")


class BotpyNotInstalled(RuntimeError):
    """qq-botpy 未安装：官方机器人功能不可用，但不影响其余模块。"""


def botpy_version() -> str:
    try:
        return importlib.metadata.version("qq-botpy")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def botpy_available() -> bool:
    """qq-botpy 是否可导入；不缓存失败结果，装好后立即生效。"""
    try:
        importlib.import_module("botpy")
    except ImportError:
        return False
    return True


_botpy_lock = threading.RLock()
_botpy_cache: dict[str, Any] = {}


def _build_client_class(botpy: Any) -> type:
    """botpy 可导入后再定义客户端类（基类必须先解析出 botpy.Client）。"""

    class LiveBotClient(botpy.Client):
        def __init__(
            self,
            *args: Any,
            on_message: RecordCallback,
            on_openid: OpenIdCallback | None = None,
            logger: logging.Logger | None = None,
            **kwargs: Any,
        ) -> None:
            super().__init__(*args, **kwargs)
            self._on_message = on_message
            self._on_openid = on_openid
            self.logger = logger or LOGGER
            self.ready = threading.Event()

        # -------------------------------------------------------------- 生命周期
        async def on_ready(self) -> None:
            robot = getattr(getattr(self, "_connection", None), "state", None)
            robot = getattr(robot, "robot", None)
            self.logger.info("QQ 机器人已连接：%s", getattr(robot, "username", "unknown"))
            self.ready.set()

        async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
            self.logger.exception("QQ 机器人事件异常：%s", event_method)

        # ---------------------------------------------------------------- 群消息
        async def on_group_message_create(self, message: Any) -> None:
            self._record_group("GROUP_MESSAGE_CREATE", message)

        async def on_group_at_message_create(self, message: Any) -> None:
            self._record_group("GROUP_AT_MESSAGE_CREATE", message)

        def _record_group(self, event: str, message: Any) -> None:
            record = {
                "msg_id": str(getattr(message, "id", "") or ""),
                "source": "qqbot",
                "event": event,
                "group_id": str(getattr(message, "group_openid", "") or ""),
                "sender_id": str(getattr(getattr(message, "author", None), "member_openid", "") or ""),
                "sender_name": str(getattr(getattr(message, "author", None), "username", "") or ""),
                "ts": iso(getattr(message, "timestamp", None)) or iso(now_local()),
                "received_at": iso(now_local()),
                "content": str(getattr(message, "content", "") or ""),
            }
            self._emit(record)

        # ---------------------------------------------------------------- 私聊
        async def on_c2c_message_create(self, message: Any) -> None:
            record = {
                "msg_id": str(getattr(message, "id", "") or ""),
                "user_openid": str(getattr(getattr(message, "author", None), "user_openid", "") or ""),
                "ts": iso(getattr(message, "timestamp", None)) or iso(now_local()),
                "content": str(getattr(message, "content", "") or ""),
            }
            self.logger.info("收到私聊，user_openid=%s（可写入 QQ_DIGEST_PUSH_OPENIDS）", record["user_openid"])
            if self._on_openid is not None:
                try:
                    self._on_openid(record)
                except Exception:  # noqa: BLE001
                    self.logger.exception("处理私聊回调失败")

        def _emit(self, record: dict[str, Any]) -> None:
            try:
                self._on_message(record)
            except Exception:  # noqa: BLE001 - 回调异常不能中断机器人连接
                self.logger.exception("处理群消息回调失败：%s", record.get("msg_id"))

    return LiveBotClient


def _load_botpy() -> dict[str, Any]:
    """延迟导入 qq-botpy 并构建依赖它的类；缺失时抛 BotpyNotInstalled。"""
    with _botpy_lock:
        if _botpy_cache:
            return _botpy_cache
        try:
            botpy = importlib.import_module("botpy")
            connection = importlib.import_module("botpy.connection")
            message = importlib.import_module("botpy.message")
        except ImportError as error:
            raise BotpyNotInstalled(MISSING_BOTPY_HINT) from error
        _botpy_cache.update(
            botpy=botpy,
            ConnectionState=connection.ConnectionState,
            GroupMessage=message.GroupMessage,
            C2CMessage=message.C2CMessage,
            LiveBotClient=_build_client_class(botpy),
        )
        return _botpy_cache


def __getattr__(name: str) -> Any:
    """兼容 `from .bot import LiveBotClient` 这类用法（PEP 562），仍然是延迟导入。"""
    if name in _LAZY_NAMES:
        return _load_botpy()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def install_full_group_message_parser() -> bool:
    """qq-botpy 1.2.1 未内置 GROUP_MESSAGE_CREATE 解析，这里补上。"""
    namespace = _load_botpy()
    connection_state = namespace["ConnectionState"]
    group_message = namespace["GroupMessage"]
    if hasattr(connection_state, "parse_group_message_create"):
        return False

    def parse_group_message_create(self: Any, payload: dict[str, Any]) -> None:
        message = group_message(self.api, payload.get("id"), payload.get("d", {}))
        self._dispatch("group_message_create", message)

    connection_state.parse_group_message_create = parse_group_message_create
    return True


class BotRunner:
    """在独立线程里跑 asyncio 事件循环，支持重启与优雅停止。"""

    def __init__(self, settings: Settings, on_message: RecordCallback, *, data_dir: Path | None = None, logger: logging.Logger | None = None) -> None:
        self.settings = settings
        self.on_message = on_message
        self.logger = logger or LOGGER
        self.data_dir = data_dir or settings.data_dir
        self.client: Any | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.stopped = threading.Event()
        self.stopped.set()
        self.last_error = ""

    @property
    def api(self) -> Any:
        return self.client.api if self.client else None

    @property
    def is_alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    @property
    def is_ready(self) -> bool:
        return bool(self.client and self.client.ready.is_set())

    def start(self) -> bool:
        if self.is_alive:
            return False
        if not self.settings.appid or not self.settings.secret:
            self.last_error = "缺少 AppID/AppSecret"
            self.logger.error("未配置 QQ_BOT_APPID/QQ_BOT_SECRET，无法启动官方机器人。")
            return False
        try:
            _load_botpy()
        except BotpyNotInstalled as error:
            self.last_error = MISSING_BOTPY_HINT
            self.logger.error("%s", error)
            return False
        self.stopped.clear()
        self.thread = threading.Thread(target=self._run, name="qq-bot", daemon=True)
        self.thread.start()
        return True

    def _run(self) -> None:
        namespace = _load_botpy()
        botpy = namespace["botpy"]
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        patched = install_full_group_message_parser()
        intents = botpy.Intents(public_messages=True)
        client = namespace["LiveBotClient"](
            intents=intents,
            timeout=10,
            is_sandbox=self.settings.sandbox,
            on_message=self.on_message,
            on_openid=self._save_openid,
            logger=self.logger,
        )
        self.client = client
        self.loop = client.loop
        self.logger.info(
            "启动 QQ 官方机器人 appid=%s sandbox=%s full_group_parser=%s botpy=%s",
            self.settings.appid[:4] + "***",
            self.settings.sandbox,
            patched,
            botpy_version(),
        )
        try:
            client.run(appid=self.settings.appid, secret=self.settings.secret)
        except BaseException as error:  # noqa: BLE001 - 线程内必须吞掉所有异常
            self.last_error = str(error)
            self.logger.exception("QQ 机器人线程退出：%s", error)
        finally:
            try:
                pending = asyncio.all_tasks(client.loop)
                for task in pending:
                    task.cancel()
            except Exception:  # noqa: BLE001
                pass
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass
            self.stopped.set()
            self.logger.info("QQ 机器人线程已退出。")

    def _save_openid(self, record: dict[str, Any]) -> None:
        path = self.data_dir / "c2c_openids.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def wait_ready(self, timeout: float = 30) -> bool:
        if not self.client:
            return False
        return self.client.ready.wait(timeout)

    def stop(self, timeout: float = 10) -> None:
        if not self.is_alive:
            return
        loop, client = self.loop, self.client
        if loop is not None and client is not None:
            try:
                future = asyncio.run_coroutine_threadsafe(client.close(), loop)
                future.result(timeout=3)
            except Exception:  # noqa: BLE001
                pass
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:  # noqa: BLE001
                pass
        if self.thread:
            self.thread.join(timeout)
        self.logger.info("QQ 机器人已停止。")
