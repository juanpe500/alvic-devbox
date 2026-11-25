"""Smoke test for the shared browser (browser.py): MCP tools, slot queue, save/restore, turn-end
release and the live view. Needs Playwright's Chromium. Agent on 127.0.0.1:7799 with
AGENT_TOKEN=testtoken; serves its own test pages on :8765."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import websockets

BASE = "http://127.0.0.1:7799"
H = {"Authorization": "Bearer testtoken"}
c = httpx.Client(base_url=BASE, headers=H, timeout=120)
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""), flush=True)
    if not cond:
        fails.append(name)


site = Path(tempfile.mkdtemp())
(site / "index.html").write_text("""<!doctype html><title>Devbox test</title>
<h1>Hello devbox</h1><input id="name"><button id="go">Go</button><p id="out"></p>
<a id="next" href="page2.html">next</a>
<script>document.getElementById('go').onclick = () => {
  document.getElementById('out').textContent = 'hi ' + document.getElementById('name').value;
  sessionStorage.setItem('x', '1'); };</script>""")
(site / "page2.html").write_text("<!doctype html><title>Page two</title><div style='height:4000px'>tall page</div>")
server = subprocess.Popen([sys.executable, "-m", "http.server", "8765", "--bind", "127.0.0.1"], cwd=site,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1)
URL = "http://localhost:8765/"

ids = iter(range(1, 10_000))


def rpc(owner, method, params=None):
    r = c.post(f"/mcp/browser/{owner}", json={"jsonrpc": "2.0", "id": next(ids), "method": method, "params": params or {}})
    return r.json()


def tool(owner, name, **args):
    res = rpc(owner, "tools/call", {"name": name, "arguments": args}).get("result", {})
    texts = [b["text"] for b in res.get("content", []) if b["type"] == "text"]
    return res, (texts[0] if texts else "")


try:
    init = rpc("alpha", "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    check("mcp initialize", init["result"]["serverInfo"]["name"] == "devbox-browser", str(init))
    check("notification → 202", c.post("/mcp/browser/alpha", json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202)
    names = [t["name"] for t in rpc("alpha", "tools/list")["result"]["tools"]]
    check("tools/list", {"browser_navigate", "browser_batch", "browser_close", "browser_screenshot"} <= set(names), str(names))

    res, text = tool("alpha", "browser_navigate", url=URL + "index.html")
    check("navigate", not res.get("isError") and "Devbox test" in text, text)
    res, text = tool("alpha", "browser_read")
    check("read", "Hello devbox" in text, text[:200])
    tool("alpha", "browser_type", selector="#name", text="Ana")
    tool("alpha", "browser_click", selector="#go")
    res, text = tool("alpha", "browser_read", selector="#out")
    check("type + click", "hi Ana" in text, text)
    res, _ = tool("alpha", "browser_screenshot")
    check("screenshot is an image", res.get("content", [{}])[0].get("type") == "image", str(res)[:200])
    res, text = tool("alpha", "browser_eval", code="document.title + ' ' + sessionStorage.getItem('x')")
    check("eval", "Devbox test 1" in text, text)
    res, text = tool("alpha", "browser_batch", steps=[
        {"action": "type", "selector": "#name", "text": "Bo"}, {"action": "click", "selector": "#go"},
        {"action": "read", "selector": "#out"}, {"action": "screenshot"}])
    check("batch", not res.get("isError") and "hi Bo" in text and any(b["type"] == "image" for b in res["content"]), text)
    res, text = tool("alpha", "browser_batch", steps=[{"action": "click", "selector": "#nope"}, {"action": "click", "selector": "#go"}])
    check("batch stops on error", res.get("isError") and "#1" not in text, text)
    res, text = tool("alpha", "browser_click", selector="#missing")
    check("error is reported, not raised", res.get("isError") is True, text)

    tool("alpha", "browser_navigate", url=URL + "page2.html")
    tool("alpha", "browser_eval", code="window.scrollTo(0, 1500)")
    res, text = tool("alpha", "browser_close")
    check("close saves", "saved" in text, text)
    st = c.get("/browser").json()
    a = next((t for t in st["tabs"] if t["owner"] == "alpha"), {})
    check("saved tab listed with preview", a.get("active") is False and a.get("has_shot") and "page2" in a.get("url", ""), str(a))
    shot = c.get("/browser/shot/alpha")
    check("preview is a jpeg", shot.status_code == 200 and shot.content[:2] == b"\xff\xd8", str(shot.status_code))
    res, text = tool("alpha", "browser_eval", code="[location.pathname, Math.round(scrollY)]")
    check("reopen restores url + scroll", "page2.html" in text and "1500" in text, text)

    # sessionStorage survives a release too
    tool("alpha", "browser_navigate", url=URL + "index.html")
    tool("alpha", "browser_click", selector="#go")
    tool("alpha", "browser_close")
    res, text = tool("alpha", "browser_eval", code="sessionStorage.getItem('x')")
    check("reopen restores sessionStorage", '"1"' in text or text == "1", text)

    # slot queue: one slot, beta waits until alpha closes
    c.post("/browser/config", json={"slots": 1})
    out = {}

    def beta():
        t = time.time()
        out["res"], out["text"] = tool("beta", "browser_navigate", url=URL + "index.html")
        out["waited"] = time.time() - t

    th = threading.Thread(target=beta)
    th.start()
    time.sleep(3)
    check("second session waits while the slot is busy", th.is_alive())
    tool("alpha", "browser_close")
    th.join(70)
    check("…and gets the slot once it frees", not out["res"].get("isError") and out["waited"] >= 2.5, str(out))
    st = c.get("/browser").json()
    check("pool status", st["slots"] == 1 and st["in_use"] == 1, str(st)[:300])

    # turn end: a Claude session's Stop hook (state idle, newer than the last use) frees its tab
    c.post("/browser/config", json={"slots": None})
    home = Path(os.environ.get("HOME", "~")).expanduser()
    reg_path = home / ".devbox" / "claude" / "sessions.json"
    reg = json.loads(reg_path.read_text()) if reg_path.exists() else {}
    reg["gamma-1"] = {"id": "gamma-1", "name": "gamma", "cwd": str(home), "uuid": "00000000-0000-0000-0000-000000000001",
                      "created_at": int(time.time()), "wanted": False}
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text(json.dumps(reg))
    tool("gamma-1", "browser_navigate", url=URL + "index.html")
    time.sleep(1.1)
    state_dir = home / ".devbox" / "claude" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "gamma-1.json").write_text(json.dumps({"state": "idle", "mode": "default", "ts": int(time.time()) + 1}))
    end = time.time() + 15
    while time.time() < end and any(t["owner"] == "gamma-1" and t["active"] for t in c.get("/browser").json()["tabs"]):
        time.sleep(1)
    g = next((t for t in c.get("/browser").json()["tabs"] if t["owner"] == "gamma-1"), {})
    check("turn end releases the tab", g.get("active") is False and g.get("name") == "gamma", str(g))
    reg = json.loads(reg_path.read_text())
    reg.pop("gamma-1", None)
    reg_path.write_text(json.dumps(reg))
    (state_dir / "gamma-1.json").unlink(missing_ok=True)
    c.delete("/browser/tabs/gamma-1")

    # live view
    async def view():
        uri = f"ws://127.0.0.1:7799/browser/view/me-test?token=testtoken&url={URL}index.html"
        async with websockets.connect(uri, max_size=None) as ws:
            got_meta = got_frame = False
            end = time.time() + 20
            while time.time() < end and not (got_meta and got_frame):
                msg = await asyncio.wait_for(ws.recv(), 20)
                if isinstance(msg, bytes):
                    got_frame = got_frame or msg[:2] == b"\xff\xd8"
                elif json.loads(msg).get("type") == "meta":
                    got_meta = got_meta or "index.html" in json.loads(msg).get("url", "")
            await ws.send(json.dumps({"type": "scroll", "dx": 0, "dy": 200}))
            await ws.send(json.dumps({"type": "navigate", "url": URL + "page2.html"}))
            seen_nav = False
            end = time.time() + 15
            while time.time() < end and not seen_nav:
                msg = await asyncio.wait_for(ws.recv(), 15)
                if isinstance(msg, str) and "page2.html" in json.loads(msg).get("url", ""):
                    seen_nav = True
            st = c.get("/browser").json()
            watched = next((t for t in st["tabs"] if t["owner"] == "me-test"), {})
            return got_meta, got_frame, seen_nav, watched.get("viewers")

    # follow mode (split screen): only attaches to an open tab, animates actions, doesn't pin it
    async def follow():
        async with websockets.connect("ws://127.0.0.1:7799/browser/view/nobody?token=testtoken&follow=1") as ws:
            closed_for_missing = json.loads(await asyncio.wait_for(ws.recv(), 10)).get("type") == "closed"
        tool("delta", "browser_navigate", url=URL + "index.html")
        async with websockets.connect("ws://127.0.0.1:7799/browser/view/delta?token=testtoken&follow=1", max_size=None) as ws:
            frame = False
            end = time.time() + 15
            while time.time() < end and not frame:
                frame = isinstance(await asyncio.wait_for(ws.recv(), 15), bytes)
            t = time.time()
            res, text = await asyncio.to_thread(lambda: tool("delta", "browser_type", selector="#name", text="Watched"))
            typed_slowly = time.time() - t > 0.5
            await asyncio.to_thread(lambda: tool("delta", "browser_click", selector="#go"))
            _, read = await asyncio.to_thread(lambda: tool("delta", "browser_read"))
            hud_hidden = "hi Watched" in read and "reading page" not in read and "clicked" not in read
            pinned = any(t["owner"] == "delta" and t["active"] for t in c.get("/browser").json()["tabs"])
            await asyncio.to_thread(lambda: tool("delta", "browser_close"))
            got_closed = False
            end = time.time() + 10
            while time.time() < end and not got_closed:
                msg = await asyncio.wait_for(ws.recv(), 10)
                got_closed = isinstance(msg, str) and json.loads(msg).get("type") == "closed"
            return closed_for_missing, frame, typed_slowly, hud_hidden, pinned, got_closed, read

    missing, fframe, slow, hidden, pinned, fclosed, read = asyncio.run(follow())
    check("follow: no open tab → closed", missing)
    check("follow: frames from an open tab", fframe)
    check("watched typing is animated (real keystrokes)", slow)
    check("HUD never shows up in browser_read", hidden, read[:200])
    check("follow: tab still open while followed", pinned)
    check("follow: told when the tab closes", fclosed)

    got_meta, got_frame, seen_nav, viewers = asyncio.run(view())
    check("live view sends page info and frames", got_meta and got_frame, f"{got_meta} {got_frame}")
    check("live view input (navigate) works", seen_nav)
    check("watched tab counts its viewer", viewers == 1, str(viewers))
    time.sleep(1)
    check("viewer leaving keeps the tab open", any(t["owner"] == "me-test" and t["active"] and t["viewers"] == 0
                                                     for t in c.get("/browser").json()["tabs"]))
    check("forget", c.delete("/browser/tabs/me-test").status_code == 200
          and not any(t["owner"] == "me-test" for t in c.get("/browser").json()["tabs"]))
    check("bad owner rejected", c.post("/mcp/browser/..%2Fx", json={}).status_code in (400, 404))
finally:
    server.terminate()

print(f"\n{len(fails)} failure(s)" if fails else "\nall browser checks passed")
sys.exit(1 if fails else 0)
