"""Smoke test for devbox/agent/agent.py against a live uvicorn on 127.0.0.1:7799."""
import asyncio
import json
import sys
import time

import httpx
import websockets

BASE = "http://127.0.0.1:7799"
TOKEN = "testtoken"
H = {"Authorization": f"Bearer {TOKEN}"}
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


c = httpx.Client(base_url=BASE, headers=H, timeout=30)
check("401 without token", httpx.get(BASE + "/health").status_code == 401)
h = c.get("/health").json()
check("health", h.get("ok") is True, str(h))

check("mkdir", c.post("/fs/mkdir", json={"path": "repos/demo/sub"}).status_code == 200)
check("write", c.post("/fs/write", json={"path": "repos/demo/app.py", "content": "print('hi')\n"}).json()["ok"])
r = c.get("/fs/read", params={"path": "repos/demo/app.py"}).json()
check("read", r["content"] == "print('hi')\n", str(r))
ls = c.get("/fs/list", params={"path": "repos/demo"}).json()
check("list", [e["name"] for e in ls["entries"]] == ["sub", "app.py"], str(ls))
check("escape blocked", c.get("/fs/read", params={"path": "../../etc/passwd"}).status_code == 400)
s = c.get("/fs/search", params={"q": "hi", "path": "repos/demo"}).json()
check("search", any("app.py" in m for m in s["matches"]), str(s))

d = c.post("/fs/delete", json={"path": "repos/demo"})
check("delete non-empty needs recursive", d.status_code == 409 and d.json()["error"] == "not_empty", d.text)

e = c.post("/exec", json={"command": "python3 -m py_compile app.py && echo OK", "cwd": "repos/demo"}).json()
check("exec", e["exit_code"] == 0 and "OK" in e["stdout"], str(e))
e = c.post("/exec", json={"command": "sleep 5", "timeout": 1}).json()
check("exec timeout", e["timed_out"] is True, str(e))

j = c.post("/jobs", json={"command": "python3 -m http.server 8765", "cwd": "repos/demo", "name": "web"}).json()
time.sleep(1.5)
ports = c.get("/ports").json()["ports"]
check("ports", 8765 in ports, str(ports))
p = c.get("/proxy/8765/app.py")
check("proxy", p.status_code == 200 and "print('hi')" in p.text, f"{p.status_code} {p.text[:200]}")
check("proxy blocks self", c.get("/proxy/7777/health").status_code == 400)
log = c.get(f"/jobs/{j['id']}").json()
check("job running", log["running"] is True, str(log))
stopped = c.post(f"/jobs/{j['id']}/stop").json()
check("job stop", stopped["running"] is False, str(stopped))

d = c.post("/fs/delete", json={"path": "repos/demo", "recursive": True})
check("delete recursive", d.status_code == 200, d.text)

u = c.post("/update").json()
st = c.get("/update").json()
check("update request leaves a file for the host watcher", u.get("ok") and st.get("pending") is True, str(st))


async def pty():
    url = f"ws://127.0.0.1:7799/pty?token={TOKEN}&cols=100&rows=30&session=smoke"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "input", "data": "echo pty-$((40+2))\r"}))
        buf = b""
        deadline = time.time() + 8
        while time.time() < deadline and b"pty-42" not in buf:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                continue
            buf += msg if isinstance(msg, bytes) else msg.encode()
        return b"pty-42" in buf


check("pty roundtrip", asyncio.run(pty()))
time.sleep(0.5)
sess = c.post("/exec", json={"command": "tmux has-session -t smoke && echo alive"}).json()
check("tmux session survives disconnect", "alive" in sess["stdout"], str(sess))
c.post("/exec", json={"command": "tmux kill-session -t smoke"})

print("\nFAILED: " + ", ".join(fails) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
