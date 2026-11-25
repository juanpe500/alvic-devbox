"""Shared headless Chromium for the devbox: Claude drives it over MCP, the phone watches it.

One persistent Chromium profile (cookies and logins are shared by every tab, like a normal
browser) and a small pool of live tabs sized from the container's RAM. Each tab belongs to an
*owner*: a Claude session id, or `me-…` for tabs the user opened from Alvic.

  * a Claude session gets a tab the first time it calls a browser_* tool; if every slot is
    taken it waits (up to a minute) for one, then is told to retry
  * a tab is released when Claude calls browser_close, when its turn ends (Stop hook), or
    after a couple of idle minutes — never while someone is watching it
  * releasing saves a screenshot (the gallery preview), the URL, scroll position and
    sessionStorage; reopening restores them (a reload with the login intact — the live JS
    state of a page can't be kept without keeping the tab)
  * the phone watches a tab live over /browser/view/{owner} (CDP screencast, screenshots as
    a fallback) and can tap, scroll, type and navigate; watching gets a slot first

Routes (bearer token like the rest of the agent):
  POST /mcp/browser/{owner}        MCP (streamable HTTP, JSON responses) — the browser_* tools
  GET  /browser                    pool status + every tab, live or saved
  GET  /browser/shot/{owner}       preview (live screenshot, else the saved one)
  POST /browser/release/{owner}    save + close the live tab (keeps the preview)
  DELETE /browser/tabs/{owner}     close it and forget its saved state
  POST /browser/config             {"slots": n | null}
  WS   /browser/view/{owner}       live view + input
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

HOME = Path(os.environ.get("HOME", "/home/dev")).resolve()
BROWSER_DIR = HOME / ".devbox" / "browser"
PROFILE = BROWSER_DIR / "profile"
TABS_DIR = BROWSER_DIR / "tabs"
CONFIG = BROWSER_DIR / "config.json"
TABS_DIR.mkdir(parents=True, exist_ok=True)

PRESETS = {"phone": {"width": 430, "height": 932}, "desktop": {"width": 1280, "height": 800}}
IDLE_RELEASE = 120          # seconds without browser use before a tab is released
STEAL_AFTER = 60            # a waiting Claude may take a tab idle this long
SLOT_WAIT = 60              # how long a tool call waits for a free slot
BROWSER_IDLE_SHUTDOWN = 600 # close Chromium itself after this long with no tabs
READ_MAX = 30_000


class Busy(Exception):
    pass


def _valid_owner(owner: str) -> str:
    if not owner or len(owner) > 60 or not all(c.isalnum() or c in "-_" for c in owner):
        raise HTTPException(400, "bad tab id")
    return owner


def default_slots() -> int:
    """Leave ~1.5 GB for Claude sessions and dev servers; ~384 MB per tab after that
    (measured: Chromium ~120 MB + 40–130 MB per tab)."""
    try:
        raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        limit_mb = None if raw == "max" else int(raw) / 2**20
    except (OSError, ValueError):
        limit_mb = None
    if not limit_mb:
        return 4
    return max(1, min(6, int((limit_mb - 1536) // 384)))


def _config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


class Tab:
    def __init__(self, owner: str, page):
        self.owner = owner
        self.page = page
        self.last_used = time.time()
        self.viewers = 0     # full-screen watchers: they keep the tab open
        self.followers = 0   # split-screen watchers: animate, but let the tab close on its own
        self.lock = asyncio.Lock()

    def watched(self) -> bool:
        return self.viewers + self.followers > 0


class Pool:
    def __init__(self):
        self.pw = None
        self.ctx = None
        self.tabs: dict[str, Tab] = {}
        self.lock = asyncio.Lock()
        self.empty_since = time.time()
        # owner id -> {"name", "state", "ts"} for Claude sessions; set by the agent.
        self.owner_info: Callable[[], dict] = lambda: {}

    def slots(self) -> int:
        n = _config().get("slots")
        return max(1, min(12, int(n))) if isinstance(n, int) else default_slots()

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def _ensure(self):
        if self.ctx is not None:
            return
        from playwright.async_api import async_playwright
        PROFILE.mkdir(parents=True, exist_ok=True)
        for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            (PROFILE / lock).unlink(missing_ok=True)   # left behind by a container restart
        if self.pw is None:
            self.pw = await async_playwright().start()
        self.ctx = await self.pw.chromium.launch_persistent_context(
            str(PROFILE), headless=True, viewport=PRESETS["phone"], device_scale_factor=2,
            has_touch=True, args=["--disable-dev-shm-usage"],
        )
        self.ctx.on("close", lambda _: self._lost())

    def _lost(self):
        self.ctx = None
        self.tabs.clear()

    async def shutdown(self):
        for owner in list(self.tabs):
            try:
                await self._release_locked(owner)
            except Exception:
                pass
        if self.ctx is not None:
            try:
                await self.ctx.close()
            except Exception:
                pass
        if self.pw is not None:
            await self.pw.stop()
        self.ctx = self.pw = None

    # ── slots ──────────────────────────────────────────────────────────────

    async def acquire(self, owner: str, wait: float = SLOT_WAIT, priority: bool = False) -> Tab:
        deadline = time.time() + wait
        while True:
            async with self.lock:
                await self._ensure()
                tab = self.tabs.get(owner)
                if tab and not tab.page.is_closed():
                    tab.last_used = time.time()
                    return tab
                self.tabs.pop(owner, None)
                if len(self.tabs) < self.slots():
                    return await self._open(owner)
                victim = self._victim(priority)
                if victim:
                    await self._release_locked(victim)
                    return await self._open(owner)
            if time.time() >= deadline:
                raise Busy(self._busy_message())
            await asyncio.sleep(1)

    def _victim(self, priority: bool) -> str | None:
        """Least recently used tab nobody is watching; a waiting Claude may only take one
        that has been idle a while, the user watching may take any."""
        now = time.time()
        free = [t for t in self.tabs.values() if t.viewers == 0 and not t.lock.locked()
                and (priority or now - t.last_used > STEAL_AFTER)]
        return min(free, key=lambda t: t.last_used).owner if free else None

    def _busy_message(self) -> str:
        info = self.owner_info()
        users = ", ".join(info.get(o, {}).get("name") or o for o in self.tabs)
        return (f"All {self.slots()} browser tabs are in use ({users}). Wait a minute and call the "
                "tool again — tabs free up when their session finishes. The user can also close one "
                "from the Browser screen in Alvic.")

    async def _open(self, owner: str) -> Tab:
        page = await self.ctx.new_page()
        tab = Tab(owner, page)
        self.tabs[owner] = tab
        self.empty_since = 0
        meta = _load_meta(owner)
        if meta.get("viewport"):
            await page.set_viewport_size(meta["viewport"])
        store = meta.get("session_storage") or {}
        if store.get("origin") and store.get("items"):
            await page.add_init_script(
                "(([origin, items]) => { if (location.origin !== origin) return;"
                " for (const [k, v] of Object.entries(items))"
                "   if (sessionStorage.getItem(k) === null) sessionStorage.setItem(k, v); })"
                f"({json.dumps([store['origin'], store['items']])})")
        url = meta.get("url") or ""
        if url and not url.startswith("about:"):
            try:
                await page.goto(url, wait_until="load", timeout=20000)
                sx, sy = (meta.get("scroll") or [0, 0])[:2]
                if sx or sy:
                    await page.evaluate("([x, y]) => window.scrollTo(x, y)", [sx, sy])
            except Exception:
                pass
        return tab

    async def release(self, owner: str):
        async with self.lock:
            await self._release_locked(owner)

    async def _release_locked(self, owner: str):
        tab = self.tabs.pop(owner, None)
        if tab is None:
            return
        try:
            await _save_state(tab)
        finally:
            try:
                await tab.page.close()
            except Exception:
                pass
            if not self.tabs:
                self.empty_since = time.time()

    async def forget(self, owner: str):
        async with self.lock:
            tab = self.tabs.pop(owner, None)
            if tab:
                try:
                    await tab.page.close()
                except Exception:
                    pass
            shutil.rmtree(TABS_DIR / owner, ignore_errors=True)

    # ── housekeeping ───────────────────────────────────────────────────────

    async def reaper(self):
        while True:
            await asyncio.sleep(5)
            try:
                await self._reap()
            except Exception:
                pass

    async def _reap(self):
        now = time.time()
        info = self.owner_info()
        for owner, tab in list(self.tabs.items()):
            if tab.viewers or tab.lock.locked():
                continue
            st = info.get(owner)
            turn_over = st is not None and st.get("state") in ("idle", "exited", "stopped") \
                and (st.get("ts") or 0) >= int(tab.last_used)
            if turn_over or now - tab.last_used > IDLE_RELEASE:
                await self.release(owner)
        if self.ctx is not None and not self.tabs and self.empty_since \
                and now - self.empty_since > BROWSER_IDLE_SHUTDOWN:
            async with self.lock:
                if not self.tabs:
                    await self.shutdown()

    def status(self) -> dict:
        info = self.owner_info()
        owners = {d.name for d in TABS_DIR.iterdir() if d.is_dir()} | set(self.tabs)
        tabs = []
        for owner in owners:
            meta = _load_meta(owner)
            live = self.tabs.get(owner)
            if live and not live.page.is_closed():
                meta = {**meta, "url": live.page.url}
            kind = "claude" if owner in info else ("me" if owner.startswith("me-") else "claude")
            tabs.append({
                "owner": owner, "kind": kind,
                "name": (info.get(owner) or {}).get("name") or meta.get("title") or owner,
                "session_state": (info.get(owner) or {}).get("state"),
                "active": live is not None, "viewers": (live.viewers + live.followers) if live else 0,
                "url": meta.get("url", ""), "title": meta.get("title", ""),
                "ts": int(live.last_used if live else meta.get("ts", 0)),
                "has_shot": live is not None or (TABS_DIR / owner / "shot.jpg").exists(),
            })
        tabs.sort(key=lambda t: (not t["active"], -t["ts"]))
        return {"slots": self.slots(), "default_slots": default_slots(), "in_use": len(self.tabs),
                "running": self.ctx is not None, "tabs": tabs}


pool = Pool()


def _load_meta(owner: str) -> dict:
    try:
        return json.loads((TABS_DIR / owner / "meta.json").read_text())
    except (OSError, ValueError):
        return {}


async def _save_state(tab: Tab):
    page = tab.page
    if page.is_closed():
        return
    folder = TABS_DIR / tab.owner
    folder.mkdir(parents=True, exist_ok=True)
    meta = {"owner": tab.owner, "url": page.url, "ts": int(time.time()),
            "viewport": page.viewport_size}
    try:
        meta["title"] = await page.title()
        meta["scroll"] = await page.evaluate("[window.scrollX, window.scrollY]")
        meta["session_storage"] = await page.evaluate(
            "({origin: location.origin, items: Object.fromEntries(Object.entries(sessionStorage))})")
    except Exception:
        pass
    try:
        await page.screenshot(path=str(folder / "shot.jpg"), type="jpeg", quality=70,
                              scale="css", timeout=5000)
    except Exception:
        pass
    tmp = folder / "meta.tmp"
    tmp.write_text(json.dumps(meta))
    tmp.replace(folder / "meta.json")


# ── tools ───────────────────────────────────────────────────────────────────

_SEL = {"type": "string", "description": "CSS selector (preferred). Scope it to the exact control."}
TOOLS = [
    {"name": "browser_navigate",
     "description": "Open a URL in your browser tab and wait for it to load. Dev servers in the devbox are "
                    "at http://localhost:<port>. Also accepts \"back\", \"forward\" or \"reload\".",
     "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "browser_read",
     "description": "Read the page: { title, url, text } with the visible text (or one element's text).",
     "inputSchema": {"type": "object", "properties": {"selector": _SEL}}},
    {"name": "browser_click",
     "description": "Click an element. ALWAYS prefer a CSS selector scoped to the intended control; "
                    "x/y are a last resort and must come from a real screenshot, never a guess. A bare "
                    "[type=submit] can hit the wrong form.",
     "inputSchema": {"type": "object", "properties": {"selector": _SEL, "x": {"type": "number"}, "y": {"type": "number"}}}},
    {"name": "browser_type",
     "description": "Type into a field: fills the element matched by selector (trusted:true sends real "
                    "keystrokes instead). submit:true presses Enter after. To fill several fields, use browser_batch.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "selector": _SEL,
                                                       "trusted": {"type": "boolean"}, "submit": {"type": "boolean"}},
                     "required": ["text"]}},
    {"name": "browser_key",
     "description": "Press one key, e.g. Enter, Tab, Escape, ArrowDown, Control+A.",
     "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
    {"name": "browser_screenshot",
     "description": "Screenshot of the tab (fullPage:true for the whole scrollable page).",
     "inputSchema": {"type": "object", "properties": {"fullPage": {"type": "boolean"}}}},
    {"name": "browser_eval",
     "description": "Run JavaScript in the page; the value of the last expression is returned (JSON).",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "browser_wait_for",
     "description": "Wait until a CSS selector is on the page (visible:true also requires it visible). "
                    "Errors after timeoutMs (default 10000).",
     "inputSchema": {"type": "object", "properties": {"selector": _SEL, "timeoutMs": {"type": "number"},
                                                       "visible": {"type": "boolean"}}, "required": ["selector"]}},
    {"name": "browser_resize",
     "description": "Resize the viewport: preset \"phone\" (430×932, the default) or \"desktop\" (1280×800), or width/height.",
     "inputSchema": {"type": "object", "properties": {"preset": {"type": "string", "enum": ["phone", "desktop"]},
                                                       "width": {"type": "number"}, "height": {"type": "number"}}}},
    {"name": "browser_list_tabs",
     "description": "The shared browser's tabs: which sessions hold one and how many slots are free.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "browser_close",
     "description": "Done with the browser for now: saves your tab (URL, scroll, preview) and frees the "
                    "slot for other sessions. Your next browser_* call reopens it where it was.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "browser_batch",
     "description": "Run several browser actions in ONE call, in order, e.g. type into 3 fields, click "
                    "submit, then screenshot/read to confirm. Stops at the first failing step and returns "
                    "the results so far. Each step is { action, ...params } with the params of the matching tool.",
     "inputSchema": {"type": "object", "properties": {"steps": {"type": "array", "items": {
         "type": "object", "properties": {
             "action": {"type": "string", "enum": ["navigate", "read", "click", "type", "key", "screenshot",
                                                   "eval", "wait_for", "resize"]},
             "selector": {"type": "string"}, "text": {"type": "string"}, "x": {"type": "number"},
             "y": {"type": "number"}, "key": {"type": "string"}, "url": {"type": "string"},
             "code": {"type": "string"}, "trusted": {"type": "boolean"}, "submit": {"type": "boolean"},
             "fullPage": {"type": "boolean"}, "visible": {"type": "boolean"}, "timeoutMs": {"type": "number"},
             "preset": {"type": "string"}, "width": {"type": "number"}, "height": {"type": "number"}},
         "required": ["action"]}}}, "required": ["steps"]}},
]

INSTRUCTIONS = (
    "A real Chromium running in this devbox, shared with the user's phone (Alvic → Browser), where "
    "the user can watch your tab live. Dev servers here are at http://localhost:<port>. Logins and "
    "cookies are shared by every tab. Tabs are limited: call browser_close when you're done so "
    "other sessions can use the browser (it also frees on its own when your turn ends).")


def _text(value) -> dict:
    return {"type": "text", "text": value if isinstance(value, str) else json.dumps(value, default=str)}


# Visual feedback while someone watches (same idea as chrome-browser's HUD): a fake cursor that
# glides to each target, smooth scrolling, highlight boxes, click ripples and toasts. It lives in
# a closed shadow root, so page CSS can't touch it and browser_read never sees its text.
HUD_JS = r"""
(() => {
  const V = 1;
  if (window.__DBHUD && window.__DBHUD.v === V) return true;
  const Z = 2147483600, HOLD = 2200, sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const host = document.createElement('devbox-hud');
  Object.assign(host.style, {position: 'fixed', inset: '0', pointerEvents: 'none', zIndex: String(Z)});
  (document.body || document.documentElement).appendChild(host);
  const root = host.attachShadow({mode: 'closed'});
  const mk = (style) => { const e = document.createElement('div'); Object.assign(e.style, style); root.appendChild(e); return e; };
  const label = (el) => (el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText)
                         || el.getAttribute('placeholder') || el.innerText || el.value
                         || el.getAttribute('name') || el.tagName || '').toString().trim().replace(/\s+/g, ' ').slice(0, 40);
  const H = {
    v: V,
    cursor() {
      if (!this.c) {
        this.c = mk({position: 'fixed', left: '50%', top: '40%', width: '22px', height: '22px', zIndex: String(Z + 5),
          transform: 'translate(-3px,-2px)', filter: 'drop-shadow(0 2px 3px rgba(0,0,0,.45))',
          transition: 'left .5s cubic-bezier(.22,.61,.36,1), top .5s cubic-bezier(.22,.61,.36,1)'});
        this.c.innerHTML = '<svg viewBox="0 0 24 24" width="22" height="22"><path d="M5 3l14 8-6 1.5L9.5 19 5 3z" fill="#fff" stroke="#111" stroke-width="1.3" stroke-linejoin="round"/></svg>';
      }
      return this.c;
    },
    pos() { const c = this.cursor(); return {x: parseFloat(c.style.left) || innerWidth / 2, y: parseFloat(c.style.top) || innerHeight * .4}; },
    async moveTo(x, y) { const c = this.cursor(); c.style.left = Math.round(x) + 'px'; c.style.top = Math.round(y) + 'px'; await sleep(480); },
    async scrollTo(el) {
      const r = el.getBoundingClientRect();
      if (r.top < 0 || r.left < 0 || r.bottom > innerHeight || r.right > innerWidth) {
        el.scrollIntoView({block: 'center', inline: 'center', behavior: 'smooth'});
        await sleep(450);
      }
    },
    async aim(el) {
      await this.scrollTo(el);
      const r = el.getBoundingClientRect();
      const p = {x: r.left + r.width / 2, y: r.top + r.height / 2};
      await this.moveTo(p.x, p.y);
      return p;
    },
    highlight(el, color, text, dur) {
      const box = mk({position: 'fixed', border: '2px solid ' + color, borderRadius: '6px', boxSizing: 'border-box',
        boxShadow: '0 0 0 3px ' + color + '33, 0 4px 18px ' + color + '55', opacity: '0', transform: 'scale(1.04)',
        transition: 'opacity .16s ease, transform .16s ease', zIndex: String(Z + 3)});
      const tag = text ? mk({position: 'fixed', font: '600 11px/1 -apple-system,Roboto,sans-serif', color: '#fff',
        background: color, padding: '3px 7px', borderRadius: '5px', whiteSpace: 'nowrap', maxWidth: '60vw',
        overflow: 'hidden', textOverflow: 'ellipsis', opacity: '0', transition: 'opacity .16s ease', zIndex: String(Z + 4)}) : null;
      if (tag) tag.textContent = text;
      const place = () => {
        const r = el.getBoundingClientRect();
        Object.assign(box.style, {left: (r.left - 3) + 'px', top: (r.top - 3) + 'px', width: (r.width + 6) + 'px', height: (r.height + 6) + 'px'});
        if (tag) Object.assign(tag.style, {left: (r.left - 3) + 'px', top: (r.top - 23) + 'px'});
      };
      place();
      addEventListener('scroll', place, true);
      requestAnimationFrame(() => { box.style.opacity = '1'; box.style.transform = 'scale(1)'; if (tag) tag.style.opacity = '1'; });
      setTimeout(() => {
        box.style.opacity = '0'; if (tag) tag.style.opacity = '0';
        setTimeout(() => { removeEventListener('scroll', place, true); box.remove(); if (tag) tag.remove(); }, 250);
      }, dur || HOLD);
    },
    frame(color) {
      const f = mk({position: 'fixed', left: '4px', top: '4px', right: '4px', bottom: '4px', border: '2px solid ' + color,
        borderRadius: '8px', opacity: '0', transition: 'opacity .2s ease', zIndex: String(Z + 2)});
      requestAnimationFrame(() => { f.style.opacity = '1'; });
      setTimeout(() => { f.style.opacity = '0'; setTimeout(() => f.remove(), 250); }, 1400);
    },
    ripple(x, y) {
      const r = mk({position: 'fixed', left: x + 'px', top: y + 'px', width: '12px', height: '12px', marginLeft: '-6px',
        marginTop: '-6px', borderRadius: '50%', border: '2px solid #22c55e', transform: 'scale(1)', opacity: '1',
        transition: 'transform .45s ease-out, opacity .45s ease-out', zIndex: String(Z + 4)});
      requestAnimationFrame(() => { r.style.transform = 'scale(3.6)'; r.style.opacity = '0'; });
      setTimeout(() => r.remove(), 480);
      const dot = mk({position: 'fixed', left: x + 'px', top: y + 'px', width: '10px', height: '10px', marginLeft: '-5px',
        marginTop: '-5px', borderRadius: '50%', background: '#22c55e', opacity: '.9',
        transition: 'opacity ' + HOLD + 'ms ease-out', zIndex: String(Z + 4)});
      requestAnimationFrame(() => { dot.style.opacity = '0'; });
      setTimeout(() => dot.remove(), HOLD + 100);
    },
    toast(text, kind) {
      const p = this.pos();
      const t = mk({position: 'fixed', left: Math.min(Math.max(p.x, 80), innerWidth - 80) + 'px', top: Math.max(p.y - 18, 34) + 'px',
        transform: 'translate(-50%,-100%) scale(.9)', padding: '5px 11px', borderRadius: '7px',
        font: '600 12px/1.2 -apple-system,Roboto,sans-serif', color: '#fff',
        background: kind === 'bad' ? '#d23c33' : kind === 'info' ? '#2563eb' : '#1f9d3f',
        boxShadow: '0 4px 14px rgba(0,0,0,.4)', opacity: '0', whiteSpace: 'nowrap', maxWidth: '80vw', overflow: 'hidden',
        textOverflow: 'ellipsis', transition: 'opacity .18s ease, transform .18s ease', zIndex: String(Z + 6)});
      t.textContent = text;
      requestAnimationFrame(() => { t.style.opacity = '1'; t.style.transform = 'translate(-50%,-100%) scale(1)'; });
      setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 220); }, HOLD);
    },
    async clickFx(el) { const p = await this.aim(el); this.highlight(el, '#22c55e', label(el)); this.ripple(p.x, p.y); await sleep(150); },
    async typeFx(el) { await this.aim(el); this.highlight(el, '#2563eb', label(el), 3500); },
    async pointFx(x, y) { await this.moveTo(x, y); this.ripple(x, y); await sleep(120); },
  };
  window.__DBHUD = H;
  H.cursor();
  return true;
})()
"""


async def _hud(page, script: str, *args):
    """Run a HUD effect; never let decoration break the real action."""
    try:
        await page.evaluate(HUD_JS)
        if args:
            await page.evaluate(script, *args)
        else:
            await page.evaluate(script)
    except Exception:
        pass


async def _hud_on(page, selector: str, effect: str):
    try:
        loc = page.locator(selector).first
        await loc.wait_for(state="visible", timeout=10000)
        await page.evaluate(HUD_JS)
        await loc.evaluate(f"(el) => window.__DBHUD && window.__DBHUD.{effect}(el)")
    except Exception:
        pass


async def _do(tab: Tab, action: str, a: dict) -> list[dict]:
    page = tab.page
    watched = tab.watched()
    if action == "navigate":
        url = (a.get("url") or "").strip()
        if url in ("back", "forward", "reload"):
            await {"back": page.go_back, "forward": page.go_forward, "reload": page.reload}[url](
                wait_until="load", timeout=30000)
        else:
            if "://" not in url and not url.startswith("about:"):
                url = ("http://" if url.startswith(("localhost", "127.")) else "https://") + url
            await page.goto(url, wait_until="load", timeout=30000)
        if watched:
            await _hud(page, "(t) => window.__DBHUD.toast(t, 'info')", "opened " + (page.url.split("/")[2] if "://" in page.url else page.url))
        return [_text({"url": page.url, "title": await page.title()})]
    if action == "read":
        if watched:
            await _hud(page, "() => { window.__DBHUD.frame('#a855f7'); window.__DBHUD.toast('reading page', 'info'); }")
            if a.get("selector"):
                await _hud_on(page, a["selector"], "typeFx")
        if a.get("selector"):
            text = await page.inner_text(a["selector"], timeout=10000)
        else:
            text = await page.evaluate("document.body ? document.body.innerText : ''")
        return [_text({"title": await page.title(), "url": page.url, "text": text[:READ_MAX]})]
    if action == "click":
        if a.get("selector"):
            if watched:
                await _hud_on(page, a["selector"], "clickFx")
            await page.click(a["selector"], timeout=10000)
        elif a.get("x") is not None and a.get("y") is not None:
            if watched:
                await _hud(page, "([x, y]) => window.__DBHUD.pointFx(x, y)", [float(a["x"]), float(a["y"])])
            await page.mouse.click(float(a["x"]), float(a["y"]))
        else:
            raise ValueError("click needs a selector (or x,y from a real screenshot)")
        return [_text("clicked")]
    if action == "type":
        text = str(a.get("text", ""))
        sel = a.get("selector")
        if sel and watched:
            await _hud_on(page, sel, "typeFx")
        if sel and not a.get("trusted") and not (watched and len(text) <= 200):
            await page.fill(sel, text, timeout=10000)
        else:
            if sel:
                if not a.get("trusted"):
                    await page.fill(sel, "", timeout=10000)   # replace, like fill
                await page.click(sel, timeout=10000)
            # Watched: real keystrokes at a readable pace, so you see it being typed.
            await page.keyboard.type(text, delay=35 if watched else 0)
        if a.get("submit"):
            await page.keyboard.press("Enter")
        return [_text("typed")]
    if action == "key":
        if watched:
            await _hud(page, "(k) => window.__DBHUD.toast('⌨ ' + k, 'info')", str(a.get("key") or "Enter"))
        await page.keyboard.press(str(a.get("key") or "Enter"))
        return [_text("pressed " + str(a.get("key")))]
    if action == "screenshot":
        data = await page.screenshot(type="jpeg", quality=80, scale="css", full_page=bool(a.get("fullPage")))
        if watched:
            await _hud(page, "() => window.__DBHUD.frame('#f59e0b')")
        return [{"type": "image", "data": base64.b64encode(data).decode(), "mimeType": "image/jpeg"}]
    if action == "eval":
        value = await page.evaluate(str(a.get("code") or "undefined"))
        return [_text(value)]
    if action == "wait_for":
        start = time.time()
        if watched:
            await _hud(page, "(s) => window.__DBHUD.toast('waiting for ' + s, 'info')", str(a["selector"])[:60])
        await page.wait_for_selector(a["selector"], state="visible" if a.get("visible") else "attached",
                                     timeout=float(a.get("timeoutMs") or 10000))
        return [_text({"found": True, "waitedMs": int((time.time() - start) * 1000)})]
    if action == "resize":
        size = PRESETS.get(a.get("preset") or "") or {"width": int(a.get("width") or 430),
                                                       "height": int(a.get("height") or 932)}
        await page.set_viewport_size(size)
        return [_text(size)]
    raise ValueError("unknown action " + action)


async def call_tool(owner: str, name: str, args: dict) -> dict:
    if name == "browser_list_tabs":
        st = pool.status()
        return {"content": [_text({"slots": st["slots"], "in_use": st["in_use"], "you": owner,
                                   "tabs": [{k: t[k] for k in ("owner", "name", "active", "url", "title")}
                                            for t in st["tabs"]]})]}
    if name == "browser_close":
        await pool.release(owner)
        return {"content": [_text("closed — your tab is saved and the slot is free")]}
    try:
        tab = await pool.acquire(owner)
    except Busy as e:
        return {"isError": True, "content": [_text(str(e))]}
    except Exception as e:
        return {"isError": True, "content": [_text(f"browser failed to start: {e}")]}
    async with tab.lock:
        tab.last_used = time.time()
        try:
            if name == "browser_batch":
                steps = args.get("steps") or []
                if not steps:
                    return {"isError": True, "content": [_text("browser_batch: steps must be a non-empty array")]}
                content, summary, failed = [], [], False
                for i, step in enumerate(steps):
                    act = step.get("action", "")
                    try:
                        out = await _do(tab, act, step)
                    except Exception as e:
                        summary.append(f"#{i} {act}: ERROR {e}")
                        failed = True
                        break
                    for block in out:
                        if block["type"] == "image":
                            content.append(block)
                            summary.append(f"#{i} {act}: ok")
                        else:
                            summary.append(f"#{i} {act}: {block['text'][:2000]}")
                content.insert(0, _text(("batch stopped on error\n" if failed else "batch ok\n") + "\n".join(summary)))
                return {"isError": failed, "content": content}
            if not name.startswith("browser_"):
                raise ValueError("unknown tool " + name)
            return {"content": await _do(tab, name[len("browser_"):], args)}
        except Exception as e:
            return {"isError": True, "content": [_text(str(e).splitlines()[0][:500] if str(e) else type(e).__name__)]}
        finally:
            tab.last_used = time.time()


# ── routes ──────────────────────────────────────────────────────────────────

router = APIRouter()


@router.post("/mcp/browser/{owner}")
async def mcp(owner: str, request: Request):
    _valid_owner(owner)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}, 400)
    batch = isinstance(body, list)
    replies = [r for r in [await _rpc(owner, m) for m in (body if batch else [body])] if r is not None]
    if not replies:
        return Response(status_code=202)
    return JSONResponse(replies if batch else replies[0])


@router.get("/mcp/browser/{owner}")
def mcp_stream(owner: str):
    return Response(status_code=405)


@router.delete("/mcp/browser/{owner}")
def mcp_end(owner: str):
    return Response(status_code=200)


async def _rpc(owner: str, msg: dict) -> dict | None:
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None     # notification (e.g. notifications/initialized)
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "devbox-browser", "version": "1"},
                  "instructions": INSTRUCTIONS}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = await call_tool(owner, params.get("name", ""), params.get("arguments") or {})
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


@router.get("/browser")
def browser_status():
    return pool.status()


@router.get("/browser/shot/{owner}")
async def browser_shot(owner: str):
    _valid_owner(owner)
    tab = pool.tabs.get(owner)
    if tab and not tab.page.is_closed():
        try:
            data = await tab.page.screenshot(type="jpeg", quality=70, scale="css", timeout=5000)
            return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
        except Exception:
            pass
    shot = TABS_DIR / owner / "shot.jpg"
    if not shot.exists():
        raise HTTPException(404, "no preview")
    return Response(shot.read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/browser/release/{owner}")
async def browser_release(owner: str):
    await pool.release(_valid_owner(owner))
    return {"ok": True}


@router.delete("/browser/tabs/{owner}")
async def browser_forget(owner: str):
    await pool.forget(_valid_owner(owner))
    return {"ok": True}


@router.post("/browser/config")
async def browser_config(request: Request):
    body = await request.json()
    slots = body.get("slots")
    cfg = _config()
    if slots is None:
        cfg.pop("slots", None)
    else:
        cfg["slots"] = max(1, min(12, int(slots)))
    CONFIG.write_text(json.dumps(cfg))
    return pool.status()


async def view_socket(ws: WebSocket, owner: str):
    """Live view: JPEG frames as binary messages, page info as JSON; input comes back as JSON
    {type: tap|scroll|text|key|navigate|back|forward|reload|resize}. Coordinates are 0–1.

    ?follow=1 (the split screen under a Claude session): only attaches to a tab that is already
    open, doesn't keep it open, and gets {type: closed} when the tab is released."""
    _valid_owner(owner)
    await ws.accept()
    follow = ws.query_params.get("follow") == "1"
    if follow:
        tab = pool.tabs.get(owner)
        if tab is None or tab.page.is_closed():
            await ws.send_json({"type": "closed"})
            await ws.close()
            return
    else:
        try:
            tab = await pool.acquire(owner, wait=15, priority=True)
        except Busy as e:
            await ws.send_json({"type": "error", "message": str(e)})
            await ws.close()
            return
        except Exception as e:
            await ws.send_json({"type": "error", "message": f"browser failed to start: {e}"})
            await ws.close()
            return
    if follow:
        tab.followers += 1
    else:
        tab.viewers += 1
    page = tab.page
    first_url = ws.query_params.get("url")
    last = {"hash": "", "at": 0.0}
    sending = asyncio.Lock()

    async def send_frame(data: bytes):
        digest = hashlib.md5(data).hexdigest()
        if digest == last["hash"]:
            return
        last["hash"], last["at"] = digest, time.time()
        async with sending:
            await ws.send_bytes(data)

    async def send_meta():
        try:
            title = await page.title()
        except Exception:
            title = ""
        vp = page.viewport_size or PRESETS["phone"]
        await ws.send_json({"type": "meta", "url": page.url, "title": title,
                            "width": vp["width"], "height": vp["height"]})

    cdp = None
    try:
        cdp = await page.context.new_cdp_session(page)

        async def on_frame(event):
            try:
                await cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
                await send_frame(base64.b64decode(event["data"]))
            except Exception:
                pass

        cdp.on("Page.screencastFrame", lambda e: asyncio.ensure_future(on_frame(e)))
        await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 60, "everyNthFrame": 1})
    except Exception:
        cdp = None

    async def fallback_frames():
        # The screencast only sends frames when the page repaints; make sure the viewer
        # always has a current picture (and gets one at all if screencast isn't available).
        while True:
            await asyncio.sleep(1.0)
            if page.is_closed() or pool.tabs.get(owner) is not tab:
                try:
                    await ws.send_json({"type": "closed"})
                    await ws.close()
                except Exception:
                    pass
                return
            if time.time() - last["at"] > 1.5:
                try:
                    await send_frame(await page.screenshot(type="jpeg", quality=60, timeout=3000))
                except Exception:
                    pass

    async def safe_meta():
        try:
            await send_meta()
        except Exception:
            pass

    def on_nav(frame):
        if frame == page.main_frame:
            asyncio.ensure_future(safe_meta())

    page.on("framenavigated", on_nav)
    pump = asyncio.create_task(fallback_frames())
    try:
        if first_url:
            try:
                await _do(tab, "navigate", {"url": first_url})
            except Exception as e:
                await ws.send_json({"type": "error", "message": str(e)[:300]})
        await send_meta()
        await send_frame(await page.screenshot(type="jpeg", quality=60))
        while True:
            msg = await ws.receive_json()
            tab.last_used = time.time()
            kind = msg.get("type")
            vp = page.viewport_size or PRESETS["phone"]
            try:
                if kind == "tap":
                    await page.touchscreen.tap(float(msg["x"]) * vp["width"], float(msg["y"]) * vp["height"])
                elif kind == "scroll":
                    await page.mouse.move(vp["width"] / 2, vp["height"] / 2)
                    await page.mouse.wheel(float(msg.get("dx") or 0), float(msg.get("dy") or 0))
                elif kind == "text":
                    await page.keyboard.type(str(msg.get("text") or ""))
                elif kind == "key":
                    await page.keyboard.press(str(msg.get("key") or "Enter"))
                elif kind in ("navigate", "back", "forward", "reload"):
                    url = msg.get("url") if kind == "navigate" else kind
                    async with tab.lock:
                        await _do(tab, "navigate", {"url": url})
                elif kind == "resize":
                    await _do(tab, "resize", msg)
                    await send_meta()
            except Exception as e:
                await ws.send_json({"type": "error", "message": (str(e).splitlines() or [""])[0][:300]})
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass
    finally:
        pump.cancel()
        try:
            page.remove_listener("framenavigated", on_nav)
        except Exception:
            pass
        if follow:
            tab.followers = max(0, tab.followers - 1)
        else:
            tab.viewers = max(0, tab.viewers - 1)
            tab.last_used = time.time()
        if cdp is not None:
            try:
                await cdp.send("Page.stopScreencast")
                await cdp.detach()
            except Exception:
                pass
