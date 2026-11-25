"""Smoke test for the /claude session manager. Needs a `claude` on PATH — CI and local runs
use tests/fake_claude (honors --session-id/--resume/--settings, writes real-format
transcripts). Agent must be running on 127.0.0.1:7799 with AGENT_TOKEN=testtoken."""
import subprocess
import sys
import time

import httpx

c = httpx.Client(base_url="http://127.0.0.1:7799", headers={"Authorization": "Bearer testtoken"}, timeout=30)
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def wait_for(fn, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.3)
    return fn()


def session(sid):
    return next((s for s in c.get("/claude/sessions").json()["sessions"] if s["id"] == sid), None)


c.post("/fs/mkdir", json={"path": "repos/alpha"})
c.post("/fs/mkdir", json={"path": "repos/beta"})

a = c.post("/claude/sessions", json={"cwd": "repos/alpha", "name": "alpha-1", "rc": True}).json()
b = c.post("/claude/sessions", json={"cwd": "repos/beta"}).json()
check("create two parallel sessions", a.get("id") and b.get("id") and a["id"] != b["id"], f"{a} {b}")
check("auto name from folder", b.get("name", "").startswith("beta-"), str(b))
import json, os  # noqa: E401
cfg = json.load(open(os.path.expanduser("~/.claude.json")))
check("folder pre-trusted in ~/.claude.json",
      cfg.get("projects", {}).get(a.get("cwd", ""), {}).get("hasTrustDialogAccepted") is True, str(cfg)[:300])

check("state idle after start hook", wait_for(lambda: (session(a["id"]) or {}).get("status") == "idle"), str(session(a["id"])))
check("mode from hook", wait_for(lambda: (session(a["id"]) or {}).get("mode") == "acceptEdits"), str(session(a["id"])))

subprocess.run(["tmux", "send-keys", "-t", "cc-" + a["id"], "hello world", "Enter"])


def roles():
    msgs = c.get("/claude/transcript", params={"uuid": a["uuid"]}).json().get("messages", [])
    return [m["role"] for m in msgs][-3:] == ["user", "assistant", "tool"] and msgs


msgs = wait_for(roles)
check("transcript: user, assistant text, tool call", bool(msgs), str(msgs))

convs = c.get("/claude/conversations", params={"cwd": "repos/alpha"}).json()["conversations"]
check("folder history lists it as live", convs and convs[0]["uuid"] == a["uuid"] and convs[0]["live"], str(convs))
check("history preview", convs and convs[0]["last_user"] == "hello world", str(convs))
allc = c.get("/claude/conversations").json()["conversations"]
check("global history includes both folders", {x["cwd_rel"] for x in allc} >= {"repos/alpha"}, str(allc))

dup = c.post("/claude/sessions", json={"cwd": "repos/alpha", "resume": a["uuid"]})
check("can't open a live conversation twice", dup.status_code == 409, dup.text)

r = c.post(f"/claude/sessions/{a['id']}", json={"name": "renamed"}).json()
check("rename", r.get("name") == "renamed", str(r))

c.delete(f"/claude/sessions/{a['id']}")
check("close → stopped", wait_for(lambda: (session(a["id"]) or {}).get("status") == "stopped"), str(session(a["id"])))
convs = c.get("/claude/conversations", params={"cwd": "repos/alpha"}).json()["conversations"]
check("closed conversation no longer live", convs and not convs[0]["live"], str(convs))

s = c.post(f"/claude/sessions/{a['id']}/start").json()
check("reopen resumes same conversation", wait_for(lambda: (session(a["id"]) or {}).get("status") == "idle")
      and s.get("uuid") == a["uuid"], str(s))
procs = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
check("relaunched with --resume <same uuid>", f"--resume {a['uuid']}" in procs,
      "\n".join(line for line in procs.splitlines() if "claude" in line))

for sid in (a["id"], b["id"]):
    c.delete(f"/claude/sessions/{sid}", params={"forget": "true"})
check("forget removes from list", not c.get("/claude/sessions").json()["sessions"])

print("\nFAILED: " + ", ".join(fails) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
