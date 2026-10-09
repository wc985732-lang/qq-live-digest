"""PWA 离线能力（Roadmap A16）回归测试。

守住：manifest 可安装、service worker 缓存壳 + 数据（离线只读）、页面有离线横幅
与安装引导，并且离线时写操作被拦下。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.store import Store  # noqa: E402
from qq_live_digest.webapp import MANIFEST_JSON, PAGE_HTML, SERVICE_WORKER_JS, TaskWebServer  # noqa: E402


class ManifestTest(unittest.TestCase):
    def test_manifest_is_installable(self) -> None:
        manifest = json.loads(MANIFEST_JSON)
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["scope"], "/")
        self.assertTrue(manifest["icons"])
        self.assertTrue(any(icon["src"] == "/icon.svg" for icon in manifest["icons"]))
        self.assertTrue(any("maskable" in icon["purpose"] for icon in manifest["icons"]))

    def test_page_links_manifest_and_ios_metas(self) -> None:
        self.assertIn('<link rel="manifest" href="/manifest.webmanifest">', PAGE_HTML)
        self.assertIn('name="theme-color"', PAGE_HTML)
        self.assertIn('name="mobile-web-app-capable"', PAGE_HTML)
        self.assertIn('name="apple-mobile-web-app-capable"', PAGE_HTML)


class ServiceWorkerTest(unittest.TestCase):
    def test_caches_shell_and_data(self) -> None:
        self.assertIn("qq-digest-shell-v", SERVICE_WORKER_JS)
        self.assertIn("qq-digest-data-v", SERVICE_WORKER_JS)
        self.assertIn("'/api/tasks'", SERVICE_WORKER_JS)

    def test_only_handles_same_origin_get(self) -> None:
        self.assertIn("event.request.method !== 'GET'", SERVICE_WORKER_JS)
        self.assertIn("url.origin !== self.location.origin", SERVICE_WORKER_JS)
        self.assertIn("url.pathname.startsWith('/api/')", SERVICE_WORKER_JS)

    def test_offline_fallbacks(self) -> None:
        self.assertIn("caches.match(event.request)", SERVICE_WORKER_JS)
        self.assertIn("caches.match('/')", SERVICE_WORKER_JS)
        self.assertIn("self.skipWaiting()", SERVICE_WORKER_JS)


class PageBehaviourTest(unittest.TestCase):
    def test_offline_banner_present_and_hidden(self) -> None:
        self.assertIn('id="offline"', PAGE_HTML)
        self.assertIn("hidden>离线：只读缓存", PAGE_HTML)

    def test_online_offline_handlers_and_write_guard(self) -> None:
        self.assertIn("window.addEventListener('online'", PAGE_HTML)
        self.assertIn("window.addEventListener('offline'", PAGE_HTML)
        self.assertIn("function isOffline()", PAGE_HTML)
        self.assertEqual(PAGE_HTML.count("if (isOffline())"), 2)

    def test_install_prompt_present(self) -> None:
        self.assertIn('id="install"', PAGE_HTML)
        self.assertIn("beforeinstallprompt", PAGE_HTML)
        self.assertIn("deferredInstall.prompt()", PAGE_HTML)
        self.assertIn("appinstalled", PAGE_HTML)

    def test_service_worker_registers_on_localhost_too(self) -> None:
        self.assertIn("canRunSW", PAGE_HTML)
        self.assertIn("location.hostname === 'localhost'", PAGE_HTML)
        self.assertIn("navigator.serviceWorker.register('/sw.js')", PAGE_HTML)


class HttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "pwa.sqlite3")
        self.settings = Settings(web_host="127.0.0.1", web_port=0, web_token="secret")
        self.server = TaskWebServer(self.settings, self.store)
        self.assertTrue(self.server.start())
        self.addCleanup(self.server.stop)
        assert self.server.server is not None
        self.base = f"http://127.0.0.1:{self.server.server.server_address[1]}"

    def test_sw_served_with_scope_header(self) -> None:
        with urllib.request.urlopen(self.base + "/sw.js", timeout=5) as response:
            self.assertEqual(response.headers["Service-Worker-Allowed"], "/")
            body = response.read().decode("utf-8")
        self.assertIn("qq-digest-shell-v", body)

    def test_manifest_parses_over_http(self) -> None:
        with urllib.request.urlopen(self.base + "/manifest.webmanifest", timeout=5) as response:
            manifest = json.loads(response.read().decode("utf-8"))
        self.assertEqual(manifest["display"], "standalone")


if __name__ == "__main__":
    unittest.main()
