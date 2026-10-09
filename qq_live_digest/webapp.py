"""手机端待办台：一个极小的 HTTP 服务，读取 tasks 表并支持勾选完成。"""

from __future__ import annotations

import datetime as dt
import hmac
import json
import logging
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import confidence
from . import conflicts
from . import ics
from . import observe
from . import restapi
from . import taskstatus
from .config import Settings
from .store import Store
from .timeutil import iso, now_local, parse_iso

LOGGER = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024

MANIFEST_JSON = json.dumps(
    {
        "name": "群消息待办",
        "short_name": "群待办",
        "description": "QQ 群通知摘要与个人待办台",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#0b0d12",
        "theme_color": "#0b0d12",
        "orientation": "portrait",
        "icons": [
            {
                "src": "/icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable",
            }
        ],
    },
    ensure_ascii=False,
)

ICON_SVG = """<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 512 512\">
  <defs>
    <linearGradient id=\"g\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"1\">
      <stop offset=\"0\" stop-color=\"#4f7cff\"/>
      <stop offset=\"1\" stop-color=\"#8b5cf6\"/>
    </linearGradient>
  </defs>
  <rect width=\"512\" height=\"512\" rx=\"112\" fill=\"#0b0d12\"/>
  <rect x=\"64\" y=\"64\" width=\"384\" height=\"384\" rx=\"96\" fill=\"url(#g)\"/>
  <path d=\"M168 264l58 58 122-142\" fill=\"none\" stroke=\"#fff\" stroke-width=\"36\" stroke-linecap=\"round\" stroke-linejoin=\"round\"/>
</svg>"""

SERVICE_WORKER_JS = """const CACHE = 'qq-digest-shell-v7';
const DATA = 'qq-digest-data-v7';
const ASSETS = ['/manifest.webmanifest', '/icon.svg'];
// 只读数据接口：网络优先，成功就留一份，断网时回放最后一次（离线只读）
const DATA_PATHS = ['/api/tasks', '/api/notices', '/api/notifications', '/api/deadlines', '/api/meta'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE && key !== DATA).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (DATA_PATHS.includes(url.pathname)) {
    event.respondWith(fetch(event.request).then(response => {
      if (response.ok) {
        const copy = response.clone();
        caches.open(DATA).then(cache => cache.put(event.request, copy));
      }
      return response;
    }).catch(() => caches.match(event.request)));
    return;
  }
  if (url.pathname.startsWith('/api/')) return;
  event.respondWith(fetch(event.request).then(response => {
    if (response.ok && (url.pathname === '/' || ASSETS.includes(url.pathname))) {
      const copy = response.clone();
      caches.open(CACHE).then(cache => cache.put(event.request, copy));
    }
    return response;
  }).catch(() => caches.match(event.request).then(cached => cached || caches.match('/'))));
});
"""
PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0d12">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>群消息待办</title>
<style>
:root{
  --bg:#0a0c12;--bg-top:#111728;--surface:rgba(255,255,255,.055);--surface-2:rgba(255,255,255,.08);
  --line:rgba(255,255,255,.09);--line-strong:rgba(255,255,255,.14);
  --text:#f3f5fa;--muted:#9aa4b5;--dim:#727d90;
  --grad:linear-gradient(135deg,#4f7cff,#8b5cf6);
  --urgent:#ff5b63;--action:#ffb020;--academic:#5b8cff;--info:#94a3b8;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{width:100%;max-width:100%;overflow-x:clip;background:var(--bg)}
body{width:100%;max-width:100%;margin:0;overflow-x:clip;background:linear-gradient(180deg,var(--bg-top),var(--bg) 340px);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;padding-bottom:calc(102px + env(safe-area-inset-bottom))}
header{width:100%;min-width:0;padding:calc(12px + env(safe-area-inset-top)) 14px 10px;position:sticky;top:0;background:linear-gradient(180deg,rgba(10,12,18,.98),rgba(10,12,18,.9));backdrop-filter:blur(16px);z-index:5}
.hero{width:100%;min-width:0;max-width:100%;border-radius:8px;padding:15px 15px 13px;background:linear-gradient(135deg,rgba(79,124,255,.3),rgba(139,92,246,.16) 60%,rgba(255,255,255,.05));border:1px solid var(--line-strong);box-shadow:0 14px 34px rgba(0,0,0,.22)}
.hero-top{display:flex;min-width:0;align-items:flex-start;justify-content:space-between;gap:12px}
.hero h1{min-width:0;margin:0;font-size:19px;line-height:1.25;font-weight:700;letter-spacing:0}
.hero p{margin:6px 0 0;color:var(--muted);font-size:12px;line-height:1.45}
.progress-label{flex:0 0 auto;font-size:12px;font-weight:650;color:#dce5ff;background:rgba(255,255,255,.08);border:1px solid var(--line);border-radius:99px;padding:3px 8px}
.bar{height:4px;border-radius:99px;background:rgba(255,255,255,.1);margin-top:12px;overflow:hidden}
.bar>i{display:block;height:100%;width:0;background:var(--grad);border-radius:99px;transition:width .35s ease}
.stats{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.stat{display:none;font-size:11px;color:#c9d3e8;background:rgba(255,255,255,.07);border:1px solid var(--line);border-radius:99px;padding:3px 8px}
.stat.show{display:inline-block}
main{width:100%;min-width:0;padding:4px 14px 30px}
.section{width:100%;min-width:0;max-width:100%;margin-top:18px}
.section-head{display:flex;min-width:0;align-items:center;justify-content:space-between;gap:10px;margin:0 0 8px;padding:0 2px}
.section-title{display:flex;align-items:center;gap:7px;margin:0;font-size:13px;font-weight:650;color:#c9d1df}
.section-title:before{content:"";width:6px;height:6px;border-radius:50%;background:var(--academic);box-shadow:0 0 0 3px rgba(91,140,255,.12)}
.section.overdue .section-title:before{background:var(--urgent);box-shadow:0 0 0 3px rgba(255,91,99,.12)}
.section.done .section-title:before{background:var(--info);box-shadow:none}
.count-pill{font-size:11px;color:var(--muted);background:rgba(255,255,255,.055);border:1px solid var(--line);border-radius:99px;padding:2px 7px}
ul{width:100%;min-width:0;list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px}
.task{position:relative;display:flex;width:100%;min-width:0;max-width:100%;gap:10px;padding:12px 12px 12px 13px;background:var(--surface);border:1px solid var(--line);border-radius:8px;overflow:hidden;transition:background .18s ease,border-color .18s ease,opacity .18s ease,transform .12s ease}
.task:before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--info)}
.task.urgent:before{background:var(--urgent)}
.task.action:before{background:var(--action)}
.task.academic:before{background:var(--academic)}
.task.overdue{background:linear-gradient(90deg,rgba(255,91,99,.13),rgba(255,255,255,.05) 42%);border-color:rgba(255,91,99,.26)}
.task.overdue:before{background:var(--urgent)}
.task.done{opacity:.48}
.task.done .t{text-decoration:line-through}
.check{position:relative;flex:0 0 auto;width:29px;height:29px;margin:0;padding:0;border-radius:50%;border:2px solid rgba(226,233,247,.68);background-color:rgba(255,255,255,.085);background-image:radial-gradient(circle at 32% 24%,rgba(255,255,255,.2),transparent 48%);box-shadow:0 0 0 3px rgba(255,255,255,.035),inset 0 1px 0 rgba(255,255,255,.16),0 4px 12px rgba(0,0,0,.2);cursor:pointer;transition:transform .16s ease,border-color .16s ease,background-color .16s ease,box-shadow .16s ease}
.check:before{content:"";position:absolute;inset:3px;border-radius:50%;border:1px solid rgba(255,255,255,.09)}
.check:after{content:"";position:absolute;left:8px;top:5px;width:7px;height:12px;border:2.5px solid #fff;border-top:0;border-left:0;border-radius:1px;transform:rotate(42deg) scale(1);opacity:.34;transition:transform .16s ease,opacity .16s ease}
.check:hover{border-color:rgba(255,255,255,.96);background-color:rgba(255,255,255,.14);background-image:radial-gradient(circle at 32% 24%,rgba(255,255,255,.28),transparent 52%);box-shadow:0 0 0 3px rgba(124,154,255,.12),inset 0 1px 0 rgba(255,255,255,.22),0 6px 16px rgba(0,0,0,.24)}
.check:active{transform:scale(.92)}
.candidate-mark{position:relative;flex:0 0 auto;display:grid;place-items:center;width:29px;height:29px;margin:0;border-radius:50%;border:2px solid rgba(180,158,255,.82);background-color:rgba(139,92,246,.18);background-image:radial-gradient(circle at 32% 24%,rgba(255,255,255,.18),transparent 48%);box-shadow:0 0 0 3px rgba(139,92,246,.08),inset 0 1px 0 rgba(255,255,255,.14);color:#e3dcff;font-size:12px;font-weight:750}
.section-head{cursor:default}
details.section>summary{cursor:pointer}
.task.done .check{border-color:rgba(255,255,255,.62);background:var(--grad);box-shadow:0 0 0 3px rgba(124,154,255,.14),inset 0 1px 0 rgba(255,255,255,.4),0 7px 18px rgba(79,124,255,.28)}
.task.done .check:after{transform:rotate(42deg) scale(1.06);opacity:1}
.body{min-width:0;max-width:100%;flex:1}
.card-top{display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:6px}
.tag{font-size:10px;font-weight:700;letter-spacing:0;border-radius:99px;padding:2px 7px;border:1px solid var(--line);color:#cbd5e1;background:rgba(255,255,255,.055)}
.tag.urgent{color:#ffb4b7;background:rgba(255,91,99,.13);border-color:rgba(255,91,99,.24)}
.tag.action{color:#ffd08a;background:rgba(255,176,32,.12);border-color:rgba(255,176,32,.22)}
.tag.academic{color:#bcd6ff;background:rgba(91,140,255,.13);border-color:rgba(91,140,255,.25)}
.tag.candidate{color:#d5c8ff;background:rgba(167,139,250,.14);border-color:rgba(167,139,250,.25)}
.deadline-chip{display:inline-flex;align-items:center;font-size:11px;color:#ffc46b;background:rgba(255,176,32,.08);border:1px solid rgba(255,176,32,.16);border-radius:99px;padding:2px 7px}
.deadline-chip.over{color:#ff9da1;background:rgba(255,91,99,.12);border-color:rgba(255,91,99,.2)}
.overdue-chip{font-size:10px;font-weight:700;color:#fff;background:var(--urgent);border-radius:99px;padding:2px 7px}
.snooze-chip{font-size:10px;font-weight:600;color:#bcd0ff;background:rgba(120,150,255,.12);border:1px solid rgba(120,150,255,.24);border-radius:99px;padding:2px 7px}
.duplicate-note{margin-top:7px;font-size:11px;color:var(--dim)}
.t{font-size:15px;font-weight:650;line-height:1.45;letter-spacing:0;overflow-wrap:anywhere;word-break:break-word}
.context{display:flex;flex-wrap:wrap;gap:6px;margin-top:7px}
.ctx{font-size:11px;color:#b8c3d8;background:rgba(255,255,255,.045);border:1px solid var(--line);border-radius:6px;padding:3px 6px;overflow-wrap:anywhere}
.meta{display:flex;min-width:0;flex-wrap:wrap;gap:6px;margin-top:7px;font-size:11px;color:var(--muted);overflow-wrap:anywhere}
.group-chip{background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:99px;padding:2px 7px}
.confidence{font-size:12px;color:#b8b0d8;margin-top:6px}
details{margin-top:7px}
summary{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--dim);cursor:pointer;list-style:none;user-select:none}
summary::-webkit-details-marker{display:none}
summary:after{content:"›";font-size:16px;line-height:1;transform:rotate(0);transition:transform .16s ease}
details[open] summary:after{transform:rotate(90deg)}
details p{margin:7px 0 0;font-size:12px;line-height:1.6;color:var(--muted);border-left:2px solid rgba(255,255,255,.1);padding-left:8px;white-space:pre-wrap;overflow-wrap:anywhere}
.detail-list{margin:7px 0 0 18px;padding:0;font-size:12px;line-height:1.55;color:var(--muted)}
.detail-list li{margin:4px 0;overflow-wrap:anywhere}
.actions{display:flex;flex-wrap:wrap;gap:7px;margin-top:10px}
.btn{border:1px solid var(--line);background:rgba(255,255,255,.06);color:var(--text);font:inherit;font-size:12px;border-radius:7px;padding:7px 10px;cursor:pointer}
.btn.primary{border-color:transparent;background:var(--grad);color:#fff}
.btn.ghost{color:var(--muted)}
.correction{margin-top:10px;padding-top:9px;border-top:1px solid var(--line)}
.correction summary{color:#aebbd4;font-weight:650}
.correction-panel{padding-top:2px}
.correction-hint{margin-top:6px;font-size:11px;line-height:1.45;color:var(--dim)}
.correct-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;margin-top:8px}
.correct-row{display:flex;min-width:0;align-items:center;gap:7px;margin-top:7px}
.correct-btn{min-width:0;padding:7px 9px;border:1px solid var(--line);border-radius:9px;background:linear-gradient(180deg,rgba(255,255,255,.09),rgba(255,255,255,.045));color:#dbe3f2;font:inherit;font-size:12px;font-weight:600;cursor:pointer;transition:transform .15s ease,background .15s ease,border-color .15s ease}
.correct-btn.primary{border-color:rgba(118,151,255,.36);background:linear-gradient(135deg,rgba(79,124,255,.3),rgba(139,92,246,.2));color:#fff}
.correct-btn.ghost{color:var(--muted);background:rgba(255,255,255,.035)}
.correct-btn:active{transform:scale(.97)}
.correct-select,.correct-date{flex:1;min-width:0;height:35px;padding:6px 8px;border:1px solid var(--line);border-radius:9px;background-color:rgba(255,255,255,.055);color:var(--text);font:inherit;font-size:12px;color-scheme:dark}
.correct-select{appearance:none;padding-right:22px;background-image:linear-gradient(45deg,transparent 50%,#8f9bb2 50%),linear-gradient(135deg,#8f9bb2 50%,transparent 50%);background-position:calc(100% - 13px) 14px,calc(100% - 9px) 14px;background-size:4px 4px,4px 4px;background-repeat:no-repeat}
.task:target{box-shadow:0 0 0 2px rgba(167,139,250,.5)}
.empty{color:var(--dim);font-size:13px;padding:10px 2px}
.tabs{position:fixed;left:50%;bottom:calc(9px + env(safe-area-inset-bottom));display:flex;gap:4px;width:calc(100% - 28px);max-width:440px;padding:6px;transform:translateX(-50%);border:1px solid rgba(255,255,255,.16);border-radius:25px;background:linear-gradient(180deg,rgba(255,255,255,.14),rgba(255,255,255,.055)),rgba(14,18,29,.74);box-shadow:0 18px 50px rgba(0,0,0,.48),inset 0 1px 0 rgba(255,255,255,.2);backdrop-filter:blur(28px) saturate(180%);-webkit-backdrop-filter:blur(28px) saturate(180%);isolation:isolate;z-index:10}
.tabs:before{content:"";position:absolute;inset:0;border-radius:inherit;background:radial-gradient(circle at 18% -20%,rgba(255,255,255,.24),transparent 42%);pointer-events:none;z-index:0}
.tabs button{position:relative;z-index:2;flex:1;min-width:0;height:54px;padding:5px 2px;border:0;background:transparent;color:rgba(221,228,242,.6);font-family:inherit;font-size:10px;font-weight:600;cursor:pointer;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;transition:color .18s ease,transform .18s ease}
.tabs button.active{color:#fff}
.tabs button:active{transform:scale(.96)}
.tabs svg{position:relative;z-index:2;width:21px;height:21px;stroke:currentColor;fill:none;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round;transition:stroke-width .18s ease,filter .18s ease}
.tabs button>span:last-child{position:relative;z-index:2;line-height:1.15}
.tabs button.active svg{stroke-width:2.25;filter:drop-shadow(0 0 8px rgba(135,165,255,.55))}
.tabs .dot{position:absolute;z-index:1;inset:5px 4px;border:1px solid transparent;border-radius:18px;background:transparent;transition:background .2s ease,border-color .2s ease,box-shadow .2s ease}
.tabs button.active .dot{border-color:rgba(255,255,255,.15);background:linear-gradient(135deg,rgba(92,139,255,.5),rgba(139,92,246,.34));box-shadow:inset 0 1px 0 rgba(255,255,255,.24),0 8px 22px rgba(79,124,255,.24)}
.notice{padding:12px;background:var(--surface);border:1px solid var(--line);border-radius:8px}
.notice h3{margin:0;font-size:14px;font-weight:650}
.notice p{margin:6px 0 0;font-size:12px;color:var(--muted);white-space:pre-wrap}
.kv{display:flex;justify-content:space-between;gap:12px;padding:11px 0;border-bottom:1px solid var(--line);font-size:13px}
.kv span:last-child{color:var(--muted);text-align:right}
.insight-list{margin:6px 0 0;padding:0 0 0 16px;list-style:disc}
.insight-list li{margin:6px 0;font-size:12px;line-height:1.5;color:var(--muted)}
.offline{position:fixed;top:0;left:0;right:0;z-index:20;padding:9px 14px;background:#3a2a12;color:#ffcf7a;font-size:13px;text-align:center;border-bottom:1px solid rgba(255,255,255,.08)}
.install-btn{position:fixed;right:12px;bottom:78px;z-index:19;padding:10px 15px;border-radius:14px;border:1px solid var(--line-strong);background:var(--grad);color:#fff;font-size:13px;font-weight:600;box-shadow:0 8px 22px rgba(0,0,0,.35)}
body.offline-mode{padding-top:40px}
</style>
</head>
<body>
<div id="offline" class="offline" role="status" hidden>离线：只读缓存，写操作已禁用</div>
<button id="install" class="install-btn" type="button" hidden>安装到桌面</button>
<header>
  <div class="hero">
    <div class="hero-top">
      <h1 id="headline">加载中…</h1>
      <span class="progress-label" id="progressLabel">0%</span>
    </div>
    <p id="subline"></p>
    <div class="bar"><i id="progress"></i></div>
    <div class="stats">
      <span class="stat" id="stat-open"></span>
      <span class="stat" id="stat-overdue"></span>
      <span class="stat" id="stat-done"></span>
    </div>
  </div>
</header>
<main>
  <section id="tab-tasks"></section>
  <section id="tab-notices" hidden></section>
  <section id="tab-settings" hidden></section>
</main>
<nav class="tabs">
  <button data-tab="tasks" class="active" aria-label="待办">
    <span class="dot"></span>
    <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="4"/><path d="m8 12 2.6 2.6L16.5 9"/></svg>
    <span>待办</span>
  </button>
  <button data-tab="notices" aria-label="通知流">
    <span class="dot"></span>
    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M10 21h4"/></svg>
    <span>通知流</span>
  </button>
  <button data-tab="settings" aria-label="设置">
    <span class="dot"></span>
    <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6 1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/></svg>
    <span>设置</span>
  </button>
</nav>
<script>
var KEY = 'qq_digest_token';
var params = new URLSearchParams(location.search);
if (params.get('token')) {
  localStorage.setItem(KEY, params.get('token'));
  // 别把 token 留在地址栏 / 历史记录 / Referer 里，只存在 localStorage
  params.delete('token');
  var rest = params.toString();
  history.replaceState(null, '', location.pathname + (rest ? '?' + rest : '') + location.hash);
}
var token = localStorage.getItem(KEY) || '';

var canRunSW = location.protocol === 'https:' || location.hostname === 'localhost' || location.hostname === '127.0.0.1';
if ('serviceWorker' in navigator && canRunSW) {
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/sw.js').catch(function () {});
  });
}

// 离线只读：断网时页面读缓存、写操作直接拦下
var offlineBanner = document.getElementById('offline');
function setOnline(state) {
  document.body.classList.toggle('offline-mode', !state);
  if (offlineBanner) offlineBanner.hidden = state;
}
setOnline(navigator.onLine !== false);
window.addEventListener('online', function () { setOnline(true); loadTasks(); });
window.addEventListener('offline', function () { setOnline(false); });
function isOffline() { return navigator.onLine === false; }

// 安装引导：支持时显示按钮，点了调起浏览器安装
var installBtn = document.getElementById('install');
var deferredInstall = null;
window.addEventListener('beforeinstallprompt', function (event) {
  event.preventDefault();
  deferredInstall = event;
  if (installBtn) installBtn.hidden = false;
});
if (installBtn) {
  installBtn.onclick = function () {
    if (!deferredInstall) return;
    deferredInstall.prompt();
    deferredInstall = null;
    installBtn.hidden = true;
  };
}
window.addEventListener('appinstalled', function () {
  deferredInstall = null;
  if (installBtn) installBtn.hidden = true;
});

function api(path, options) {
  options = options || {};
  options.headers = Object.assign({'X-Token': token}, options.headers || {});
  return fetch(path, options).then(function (response) {
    if (!response.ok) throw new Error('HTTP ' + response.status);
    return response.json();
  });
}

function el(tag, cls, text) {
  var node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function actionButton(label, action, cls) {
  var button = el('button', 'btn ' + (cls || ''), label);
  button.onclick = function () { sendAction(button.__task, action); };
  return button;
}


var CATEGORY_LABELS = {urgent: '紧急', action: '待办', academic: '学业', info: '通知'};

function correctionButton(label, correction, cls, value) {
  var button = el('button', 'correct-btn ' + (cls || ''), label);
  button.onclick = function () {
    var actual = typeof value === 'function' ? value() : value;
    sendCorrection(button.__task, correction, actual);
  };
  return button;
}

function correctionPanel(task) {
  var box = el('details', 'correction');
  box.appendChild(el('summary', '', '纠错'));
  var panel = el('div', 'correction-panel');
  panel.appendChild(el('div', 'correction-hint', '纠正后会记录原判断，用于后续减少误判'));
  var grid = el('div', 'correct-grid');
  var quick = [['不是通知', 'not_notice'], ['不是待办', 'not_task'], ['标记重复', 'duplicate']];
  if (task.category === 'urgent') quick.splice(2, 0, ['不是紧急', 'not_urgent']);
  quick.forEach(function (item) {
    var button = correctionButton(item[0], item[1]);
    button.__task = task;
    grid.appendChild(button);
  });
  panel.appendChild(grid);

  var categoryRow = el('div', 'correct-row');
  var categorySelect = el('select', 'correct-select');
  categorySelect.setAttribute('aria-label', '修改任务分类');
  [['urgent', '紧急'], ['action', '待办'], ['academic', '学业'], ['info', '通知']].forEach(function (item) {
    var option = el('option', '', item[1]);
    option.value = item[0];
    categorySelect.appendChild(option);
  });
  categorySelect.value = CATEGORY_LABELS[task.category] ? task.category : 'info';
  var categoryButton = correctionButton('保存分类', 'category', 'primary', function () { return categorySelect.value; });
  categoryButton.__task = task;
  categoryRow.appendChild(categorySelect);
  categoryRow.appendChild(categoryButton);
  panel.appendChild(categoryRow);

  var deadlineRow = el('div', 'correct-row');
  var deadlineInput = el('input', 'correct-date');
  deadlineInput.type = 'datetime-local';
  deadlineInput.setAttribute('aria-label', '修改截止时间');
  deadlineInput.value = String(task.deadline || '').slice(0, 16);
  var deadlineButton = correctionButton('保存时间', 'deadline', 'primary', function () { return deadlineInput.value; });
  var clearButton = correctionButton('清空时间', 'clear_deadline', 'ghost');
  deadlineButton.__task = task; clearButton.__task = task;
  deadlineRow.appendChild(deadlineInput);
  deadlineRow.appendChild(deadlineButton);
  deadlineRow.appendChild(clearButton);
  panel.appendChild(deadlineRow);
  box.appendChild(panel);
  return box;
}
function taskNode(task) {
  var status = task.status || (task.done ? 'done' : 'open');
  var candidate = status === 'candidate';
  var category = CATEGORY_LABELS[task.category] ? task.category : 'info';
  var overdue = Boolean(task.overdue && !task.done);
  var classes = ['task', category];
  if (candidate) classes.push('candidate');
  if (task.done) classes.push('done');
  if (overdue) classes.push('overdue');
  var li = el('li', classes.join(' '));
  li.id = 'task-' + task.id;
  if (candidate) {
    var candidateMark = el('span', 'candidate-mark', '?');
    candidateMark.setAttribute('aria-hidden', 'true');
    li.appendChild(candidateMark);
  } else {
    var check = el('button', 'check');
    check.setAttribute('aria-label', task.done ? '恢复待办' : '标记完成');
    check.setAttribute('aria-pressed', task.done ? 'true' : 'false');
    check.title = task.done ? '恢复待办' : '标记完成';
    check.onclick = function () { toggleTask(task); };
    li.appendChild(check);
  }
  var body = el('div', 'body');
  var top = el('div', 'card-top');
  top.appendChild(el('span', 'tag ' + category, CATEGORY_LABELS[category]));
  if (candidate) top.appendChild(el('span', 'tag candidate', '待确认'));
  if (task.deadline_text) top.appendChild(el('span', 'deadline-chip' + (overdue ? ' over' : ''), task.deadline_text));
  if (overdue) top.appendChild(el('span', 'overdue-chip', '已逾期'));
  if (task.snoozed) top.appendChild(el('span', 'snooze-chip', '稍后 ' + (task.snooze_text || '')));
  body.appendChild(top);
  body.appendChild(el('div', 't', task.summary || task.text || ''));
  if (task.audience || task.condition) {
    var context = el('div', 'context');
    if (task.audience) context.appendChild(el('span', 'ctx', '适用：' + task.audience));
    if (task.condition) context.appendChild(el('span', 'ctx', '条件：' + task.condition));
    body.appendChild(context);
  }
  if (task.groups && task.groups.length) {
    var meta = el('div', 'meta');
    meta.appendChild(el('span', 'group-chip', task.groups.join('、')));
    body.appendChild(meta);
  }
  if (task.duplicate_summary) {
    body.appendChild(el('div', 'duplicate-note', '与「' + task.duplicate_summary + '」重复'));
  }
  if (candidate) {
    body.appendChild(el('div', 'confidence', (task.confidence_text || '') + ' · ' + (task.confidence_reason || '')));
    if (task.confidence_why) body.appendChild(el('div', 'confidence', '依据：' + task.confidence_why));
    var actions = el('div', 'actions');
    var confirm = actionButton('确认待办', 'confirm', 'primary');
    var dismiss = actionButton('忽略', 'dismiss', 'ghost');
    var snooze = actionButton('明天提醒', 'snooze', 'ghost');
    confirm.__task = task; dismiss.__task = task; snooze.__task = task;
    actions.appendChild(confirm); actions.appendChild(dismiss); actions.appendChild(snooze);
    body.appendChild(actions);
  }
  if (task.details && task.details.length) {
    var detailBox = el('details');
    detailBox.appendChild(el('summary', '', '详细要求'));
    var detailList = el('ul', 'detail-list');
    task.details.forEach(function (value) { detailList.appendChild(el('li', '', value)); });
    detailBox.appendChild(detailList);
    body.appendChild(detailBox);
  }
  var fullText = String(task.source_text || '').trim();
  var evidenceText = String(task.evidence || '').trim();
  if (fullText || (evidenceText && evidenceText !== task.summary)) {
    var detail = el('details');
    detail.appendChild(el('summary', '', '查看完整原文'));
    detail.appendChild(el('p', '', fullText || evidenceText));
    body.appendChild(detail);
  }
  body.appendChild(correctionPanel(task));
  li.appendChild(body);
  return li;
}

function section(title, tasks, options) {
  tasks = tasks || [];
  options = options || {};
  if (!tasks.length) return null;
  var wrap = el(options.collapsed ? 'details' : 'section', 'section ' + (options.className || ''));
  var head = el(options.collapsed ? 'summary' : 'div', 'section-head');
  head.appendChild(el('h2', 'section-title', title));
  head.appendChild(el('span', 'count-pill', tasks.length + ' 件'));
  wrap.appendChild(head);
  var list = el('ul');
  tasks.forEach(function (task) { list.appendChild(taskNode(task)); });
  wrap.appendChild(list);
  return wrap;
}

function showStat(id, text, show) {
  var node = document.getElementById(id);
  node.textContent = text;
  node.classList.toggle('show', Boolean(show));
}

function render(data) {
  var progress = Number(data.progress || 0);
  document.getElementById('headline').textContent = data.headline || '今天没有待办';
  document.getElementById('subline').textContent = data.subline || '';
  document.getElementById('progressLabel').textContent = progress + '%';
  document.getElementById('progress').style.width = progress + '%';
  var stats = data.stats || {};
  showStat('stat-open', '未完成 ' + (stats.open || 0), (stats.open || 0) > 0);
  showStat('stat-overdue', '已逾期 ' + (stats.overdue || 0), (stats.overdue || 0) > 0);
  showStat('stat-done', '已完成 ' + (stats.done || 0), (stats.done || 0) > 0);
  var today = data.today || [];
  var overdue = today.filter(function (task) { return task.overdue; });
  var dueToday = today.filter(function (task) { return !task.overdue; });
  var blocks = [
    section('已过期', overdue, {className: 'overdue'}),
    section('今天', dueToday),
    section('待确认', data.candidates || []),
    section('本周', data.week || []),
    section('以后', data.later || []),
    section('已完成', data.done || [], {className: 'done', collapsed: true})
  ].filter(Boolean);
  var root = document.getElementById('tab-tasks');
  root.textContent = '';
  if (!blocks.length) {
    root.appendChild(el('div', 'empty', '今天没有需要处理的事项'));
  } else {
    blocks.forEach(function (block) { root.appendChild(block); });
  }
  if (location.hash) {
    var focused = document.querySelector(location.hash);
    if (focused) setTimeout(function () { focused.scrollIntoView({block: 'center'}); }, 40);
  }
}

function sendAction(task, action) {
  if (isOffline()) { alert('离线状态：只读缓存，暂不能修改；连上网再试。'); return; }
  api('/api/tasks/' + task.id, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: action})
  }).then(loadTasks).catch(function (error) { alert('操作失败：' + error.message); });
}

function sendCorrection(task, correction, value) {
  if (isOffline()) { alert('离线状态：只读缓存，暂不能纠错；连上网再试。'); return; }
  api('/api/tasks/' + task.id, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'correct', correction: correction, value: value || ''})
  }).then(loadTasks).catch(function (error) { alert('纠错失败：' + error.message); });
}

function toggleTask(task) {
  sendAction(task, task.done ? 'reopen' : 'done');
}

function loadTasks() {
  api('/api/tasks').then(render).catch(function (error) {
    document.getElementById('headline').textContent = '加载失败';
    document.getElementById('subline').textContent = error.message;
  });
}

function loadNotices() {
  var root = document.getElementById('tab-notices');
  root.textContent = '';
  api('/api/notices').then(function (data) {
    var head = el('div', 'section-head');
    head.appendChild(el('h2', 'section-title', '最近推送'));
    root.appendChild(head);
    if (!data.items.length) {
      root.appendChild(el('div', 'empty', '还没有记录'));
      return;
    }
    var list = el('ul');
    data.items.forEach(function (item) {
      var card = el('li', 'notice');
      card.appendChild(el('h3', '', item.summary || item.kind || '摘要'));
      card.appendChild(el('p', '', item.body || ''));
      list.appendChild(card);
    });
    root.appendChild(list);
  });
}

function loadSettings() {
  var root = document.getElementById('tab-settings');
  root.textContent = '';
  api('/api/meta').then(function (data) {
    var head = el('div', 'section-head');
    head.appendChild(el('h2', 'section-title', '运行信息'));
    root.appendChild(head);
    var rows = [
      ['监控群', (data.groups || []).join('、') || '未配置'],
      ['推送通道', (data.channels || []).join('、') || '无'],
      ['截止提醒', data.reminders || '未开启'],
      ['推送预算', data.push_budget || '不限'],
      ['夜间静默', data.quiet_hours || '未设置'],
      ['待办统计', '共 ' + data.stats.total + ' 件，未完成 ' + data.stats.open + ' 件'],
      ['服务时间', data.started_at || '未知']
    ];
    rows.forEach(function (row) {
      var kv = el('div', 'kv');
      kv.appendChild(el('span', '', row[0]));
      kv.appendChild(el('span', '', row[1]));
      root.appendChild(kv);
    });
    var insights = data.insights || [];
    if (insights.length) {
      var tipHead = el('div', 'section-head');
      tipHead.appendChild(el('h2', 'section-title', '纠错提示'));
      root.appendChild(tipHead);
      var tipList = el('ul', 'insight-list');
      insights.forEach(function (text) { tipList.appendChild(el('li', '', text)); });
      root.appendChild(tipList);
    }
  });
}

document.querySelectorAll('.tabs button').forEach(function (button) {
  button.onclick = function () {
    document.querySelectorAll('.tabs button').forEach(function (other) { other.classList.remove('active'); });
    button.classList.add('active');
    var tab = button.getAttribute('data-tab');
    ['tasks', 'notices', 'settings'].forEach(function (name) {
      document.getElementById('tab-' + name).hidden = name !== tab;
    });
    if (tab === 'notices') loadNotices();
    if (tab === 'settings') loadSettings();
  };
});

loadTasks();
setInterval(loadTasks, 60000);
</script>
</body>
</html>
"""


def _deadline_label(value: dt.datetime | None, now: dt.datetime) -> tuple[str, bool]:
    if not isinstance(value, dt.datetime):
        return "", False
    overdue = value < now
    if value.date() == now.date():
        return (f"今天 {value:%H:%M} 已过" if overdue else f"今天 {value:%H:%M} 截止"), overdue
    if value.date() == now.date() + dt.timedelta(days=1):
        return f"明天 {value:%H:%M} 截止", False
    return f"{value:%m-%d %H:%M} 截止", overdue and value.date() < now.date()


def snooze_default_until(settings: Settings, now: dt.datetime) -> str:
    """稍后提醒的默认时间：次日早晨的截止提醒时刻，否则次日同一时间。"""
    target = now + dt.timedelta(days=1)
    if settings.deadline_reminders_enabled:
        try:
            hour, minute = (int(part) for part in str(settings.deadline_morning).split(":", 1))
        except (TypeError, ValueError):
            hour, minute = 7, 30
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            target = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return iso(target)


def group_tasks(tasks: list[dict[str, Any]], now: dt.datetime) -> dict[str, Any]:
    candidate: list[dict[str, Any]] = []
    today: list[dict[str, Any]] = []
    week: list[dict[str, Any]] = []
    later: list[dict[str, Any]] = []
    done: list[dict[str, Any]] = []
    horizon = now + dt.timedelta(days=7)
    for task in tasks:
        status = str(task.get("status") or "open")
        if status in {"dismissed", "expired"}:
            continue
        deadline = parse_iso(task.get("deadline"))
        payload = dict(task)
        payload["deadline_text"], payload["overdue"] = _deadline_label(deadline, now)
        snooze_until = parse_iso(payload.get("snooze_until"))
        payload["snoozed"] = bool(isinstance(snooze_until, dt.datetime) and snooze_until > now)
        payload["snooze_text"] = (
            snooze_until.strftime("%m-%d %H:%M") if payload["snoozed"] and snooze_until else ""
        )
        payload["done"] = status == "done"
        if status == "candidate":
            payload["confidence_text"] = confidence.confidence_text(
                float(payload.get("confidence") or 0.0)
            )
            detail = str(payload.get("candidate_detail") or "")
            try:
                parsed = json.loads(detail) if detail else {}
            except json.JSONDecodeError:
                parsed = {}
            payload["confidence_reason"] = str(parsed.get("reason") or "需要你确认后再进入正式待办。")
            payload["confidence_why"] = confidence.describe_triggers(
                parsed.get("triggers") or [], limit=3
            )
            candidate.append(payload)
            continue
        if payload["done"]:
            done.append(payload)
            continue
        if deadline is None:
            later.append(payload)
        elif deadline.date() <= now.date():
            today.append(payload)
        elif deadline <= horizon:
            week.append(payload)
        else:
            later.append(payload)
    done.sort(key=lambda item: str(item.get("done_at") or ""), reverse=True)
    return {
        "candidates": candidate[:40],
        "today": today[:40],
        "week": week[:40],
        "later": later[:40],
        "done": done[:20],
    }


def overview(tasks: list[dict[str, Any]], grouped: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    open_tasks = [task for task in tasks if str(task.get("status") or "open") == "open"]
    candidates = grouped.get("candidates") or []
    done_count = sum(1 for task in tasks if str(task.get("status") or "") == "done")
    today = grouped["today"]
    if today:
        nearest = min(
            (parse_iso(task.get("deadline")) for task in today if parse_iso(task.get("deadline"))),
            default=None,
        )
        overdue = sum(1 for task in today if task.get("overdue"))
        if overdue and overdue == len(today):
            headline = f"今天 {len(today)} 件，都已过期"
        elif overdue:
            headline = f"今天 {len(today)} 件，{overdue} 件已过期"
        elif nearest:
            headline = f"今天 {len(today)} 件，最近一件 {nearest:%H:%M} 截止"
        else:
            headline = f"今天 {len(today)} 件"
    elif candidates:
        headline = f"{len(candidates)} 条通知待确认"
    elif open_tasks:
        headline = "今天没有到期的事"
    else:
        headline = "所有待办都清空了"
    if any(task.get("overdue") for task in today):
        subline = "先处理已逾期事项，再处理今天到期的任务"
    elif today:
        subline = "按截止时间从上到下处理"
    elif candidates:
        subline = "确认后才会进入正式待办"
    elif open_tasks:
        subline = "当前没有到期事项，可按自己的节奏推进"
    else:
        subline = "可以休息一下，新的通知会自动汇总"
    actionable = len(open_tasks) + len(candidates) + done_count
    progress = round(done_count * 100 / actionable) if actionable else 0
    return {"headline": headline, "subline": subline, "progress": progress}


class _Handler(BaseHTTPRequestHandler):
    server_version = "qq-tasks/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 与基类签名一致
        LOGGER.debug("web %s", format % args)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _asset(self, body: str, content_type: str, *, cache: str = "no-store") -> None:
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", cache)
        if content_type.startswith("text/javascript"):
            self.send_header("Service-Worker-Allowed", "/")
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        token = str(getattr(self.server, "token", "") or "")
        if not token:
            return True
        expected = token.encode("utf-8")
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        supplied = list(params.get("token", [])) + [self.headers.get("X-Token", "")]
        # 恒定时间比较，避免按字符逐位泄露 token
        return any(hmac.compare_digest(str(item).encode("utf-8"), expected) for item in supplied)

    def _cross_site(self) -> bool:
        """浏览器跨站 POST 会带 Origin；与 Host 不一致（含 `null`）即判为跨站，防 CSRF。"""
        origin = str(self.headers.get("Origin") or "").strip()
        if not origin:
            return False
        host = str(self.headers.get("Host") or "").strip().lower()
        return urllib.parse.urlparse(origin).netloc.lower() != host

    @property
    def store(self) -> Store:
        return self.server.store  # type: ignore[attr-defined]

    def _query(self) -> dict[str, list[str]]:
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _query_int(self, name: str, default: int, low: int, high: int) -> int:
        raw = (self._query().get(name) or [str(default)])[0]
        return restapi.clamp_int(raw, default, low, high)

    def _query_flag(self, name: str) -> bool:
        value = str((self._query().get(name) or [""])[0]).strip().lower()
        return value in {"1", "true", "yes", "on"}

    def do_GET(self) -> None:  # noqa: N802 - 基类命名
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/health", "/api/health"):
            self._json(200, restapi.health_payload())
            return
        if path == "/manifest.webmanifest":
            self._asset(MANIFEST_JSON, "application/manifest+json; charset=utf-8")
            return
        if path == "/icon.svg":
            self._asset(ICON_SVG, "image/svg+xml; charset=utf-8", cache="public, max-age=86400")
            return
        if path == "/sw.js":
            self._asset(SERVICE_WORKER_JS, "text/javascript; charset=utf-8")
            return
        if not self._authorized():
            self._json(401, {"ok": False, "error": "invalid token"})
            return
        if path == "/api":
            self._json(200, restapi.index_payload())
            return
        if path in ("/api/notifications", "/api/notices"):
            self._json(
                200,
                restapi.notifications(
                    self.store, limit=self._query_int("limit", 8, 1, 50)
                ),
            )
            return
        if path == "/api/deadlines":
            self._json(
                200,
                restapi.deadlines(
                    self.store,
                    now=now_local(),
                    limit=self._query_int("limit", 200, 1, 1000),
                    include_done=self._query_flag("include_done"),
                ),
            )
            return
        if path == "/api/calendar.ics":
            include_done = self._query_flag("include_done")
            body = ics.build_calendar(
                self.store.list_tasks(statuses=taskstatus.statuses_for(include_done)),
                now=now_local(),
                include_done=include_done,
            )
            raw = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition", 'attachment; filename="qq-digest.ics"')
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/api/conflicts":
            include_done = self._query_flag("include_done")
            self._json(
                200,
                conflicts.payload(
                    self.store.list_tasks(statuses=taskstatus.statuses_for(include_done)),
                    now=now_local(),
                    window_minutes=self._query_int("window", conflicts.DEFAULT_WINDOW_MINUTES, 0, 1440),
                    include_done=include_done,
                ),
            )
            return
        if path in ("/", "/index.html"):
            self._html(PAGE_HTML)
            return
        if path == "/api/tasks":
            now = now_local()
            tasks = self.store.list_tasks()
            grouped = group_tasks(tasks, now)
            payload = overview(tasks, grouped, now)
            payload["stats"] = self.store.task_stats()
            payload.update(grouped)
            self._json(200, payload)
            return
        if path == "/api/insights":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                days = int((query.get("days") or ["30"])[0])
            except (TypeError, ValueError):
                days = 30
            days = max(1, min(365, days))
            self._json(
                200,
                {
                    "stats": self.store.correction_stats(days=days),
                    "insights": self.store.correction_insights(days=days),
                },
            )
            return
        if path == "/api/meta":
            settings = self.server.settings  # type: ignore[attr-defined]
            manager = getattr(self.server, "meta_provider", None)
            info: dict[str, Any] = {}
            if callable(manager):
                try:
                    info = manager() or {}
                except Exception:  # noqa: BLE001 - 元信息失败不影响页面
                    info = {}
            self._json(
                200,
                {
                    "groups": list(info.get("groups") or []),
                    "channels": list(info.get("channels") or []),
                    "reminders": info.get("reminders") or "",
                    "started_at": info.get("started_at") or "",
                    "insights": list(info.get("insights") or []),
                    "push_budget": info.get("push_budget") or "",
                    "quiet_hours": info.get("quiet_hours") or "",
                    "stats": self.store.task_stats(),
                },
            )
            return
        if path in ("/panel", "/panel.html"):
            self._html(observe.render_page())
            return
        if path == "/api/panel":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                days = int((query.get("days") or [str(observe.DEFAULT_DAYS)])[0])
            except (TypeError, ValueError):
                days = observe.DEFAULT_DAYS
            self._json(200, observe.payload(observe.snapshot(self.store, days=days)))
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - 基类命名
        if not self._authorized():
            self._json(401, {"ok": False, "error": "invalid token"})
            return
        if self._cross_site():
            self._json(403, {"ok": False, "error": "cross-site request blocked"})
            return
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if not path.startswith("/api/tasks/"):
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            task_id = int(path.rsplit("/", 1)[-1])
        except ValueError:
            self._json(400, {"ok": False, "error": "bad task id"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._json(400, {"ok": False, "error": "bad content length"})
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._json(400, {"ok": False, "error": "bad body"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"ok": False, "error": "bad json"})
            return
        action = str(payload.get("action") or "").strip().lower()
        if not action and "done" in payload:
            action = "done" if bool(payload.get("done")) else "reopen"
        if action == "correct":
            try:
                changed = self.store.apply_task_correction(
                    task_id,
                    str(payload.get("correction") or ""),
                    value=payload.get("value", ""),
                )
            except ValueError as error:
                self._json(400, {"ok": False, "error": str(error)})
                return
        else:
            if action not in {"confirm", "dismiss", "done", "reopen", "snooze"}:
                self._json(400, {"ok": False, "error": "unsupported action"})
                return
            detail: dict[str, Any] = {}
            if action == "snooze":
                settings = self.server.settings  # type: ignore[attr-defined]
                detail = {"until": snooze_default_until(settings, now_local())}
            changed = self.store.apply_task_action(task_id, action, detail=detail)
        task = self.store.get_task(task_id) if changed else None
        self._json(
            200 if changed else 404,
            {
                "ok": changed,
                "id": task_id,
                "action": action,
                "done": bool(task and task.get("status") == "done"),
                "status": (task or {}).get("status", ""),
            },
        )


class TaskWebServer:
    """待办台的 HTTP 服务，绑定在独立端口上。"""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        *,
        logger: logging.Logger | None = None,
        meta_provider: Any = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.logger = logger or LOGGER
        self.meta_provider = meta_provider
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.token = settings.web_token or store.meta_get("web_token", "")
        if not self.token:
            # 无论监听回环还是对外，都自动生成 token 并落库：绑定回环反而免鉴权是安全边界反转。
            self.token = secrets.token_urlsafe(12)
            store.meta_set("web_token", self.token)

    @property
    def is_alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self) -> bool:
        if self.is_alive:
            return True
        try:
            server = ThreadingHTTPServer((self.settings.web_host, self.settings.web_port), _Handler)
        except OSError as error:
            self.logger.error("待办台启动失败（%s:%d）：%s", self.settings.web_host, self.settings.web_port, error)
            return False
        server.store = self.store  # type: ignore[attr-defined]
        server.settings = self.settings  # type: ignore[attr-defined]
        server.token = self.token  # type: ignore[attr-defined]
        server.meta_provider = self.meta_provider  # type: ignore[attr-defined]
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, name="tasks-web", daemon=True)
        self.thread.start()
        self.logger.info(
            "待办台已启动：http://%s:%d/ （%s）",
            self.settings.web_host,
            self.settings.web_port,
            "需要 token" if self.token else "未设 token",
        )
        return True

    def stop(self) -> None:
        if self.server is not None:
            try:
                self.server.shutdown()
                if self.thread is not None:
                    self.thread.join(timeout=5)
                self.server.server_close()
            except Exception:  # noqa: BLE001 - 关闭失败不应阻塞退出
                self.logger.exception("待办台关闭失败")
        self.server = None
        self.thread = None

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.settings.web_enabled),
            "alive": self.is_alive,
            "host": self.settings.web_host,
            "port": self.settings.web_port,
            "auth": bool(self.token),
        }
