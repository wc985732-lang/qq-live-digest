"""Public web URL helpers for LAN and Tailscale access."""

from __future__ import annotations

import socket
import urllib.parse

from .config import Settings


def detect_lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def build_web_url(
    settings: Settings,
    *,
    token: str = "",
    task_id: int | None = None,
    base_url: str = "",
) -> str:
    base = (base_url or settings.web_base_url).strip().rstrip("/")
    if not base:
        host = str(settings.web_host or "127.0.0.1")
        if host in {"0.0.0.0", "::", ""}:
            host = detect_lan_ip()
        base = f"http://{host}:{settings.web_port}"

    if token:
        url = base + "/login/" + urllib.parse.quote(str(token), safe="")
    else:
        url = base + "/"
    if task_id is not None:
        url += f"#task-{int(task_id)}"
    return url
