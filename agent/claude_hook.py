#!/usr/bin/env python3
"""Claude Code hook → records a devbox Claude session's state for the agent.

Injected into every `claude` the agent launches (via --settings) as:
    python3 /opt/agent/claude_hook.py <event>     event ∈ prompt | stop | notify | start

The session id comes from DEVBOX_CC_ID, set on the session's tmux environment by
the agent. Claude passes the hook JSON on stdin; we keep `permission_mode`.
Writes ~/.devbox/claude/state/<id>.json atomically. Must be fast and never fail
loudly (a stuck hook stalls claude), so everything is best-effort and exits 0.
"""
import json
import os
import select
import sys
import time

STATE_BY_EVENT = {"prompt": "busy", "stop": "idle", "notify": "waiting", "start": "idle"}


def _is_idle_reminder(data: dict) -> bool:
    """Claude also notifies after ~60s at the prompt ("Claude is waiting for your input").
    That's just idle; "waiting" is for permission prompts and questions."""
    kind = str(data.get("notification_type") or "").lower()
    if kind:
        return kind == "idle_prompt"
    return "waiting for your input" in str(data.get("message") or "").lower()


def main():
    sid = os.environ.get("DEVBOX_CC_ID", "")
    if not sid or not sid.replace("-", "").isalnum():
        return
    event = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    mode = "default"
    data = {}
    try:
        if select.select([sys.stdin], [], [], 0.5)[0]:
            data = json.loads(sys.stdin.read(1 << 20) or "{}")
            if isinstance(data.get("permission_mode"), str) and data["permission_mode"]:
                mode = data["permission_mode"]
    except Exception:
        pass
    state = STATE_BY_EVENT.get(event, "idle")
    if event == "notify" and _is_idle_reminder(data):
        state = "idle"
    folder = os.path.join(os.path.expanduser("~"), ".devbox", "claude", "state")
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, f".{sid}.tmp")
    with open(tmp, "w") as fh:
        json.dump({"state": state, "mode": mode, "ts": int(time.time())}, fh)
    os.replace(tmp, os.path.join(folder, f"{sid}.json"))


try:
    main()
except Exception:
    pass
sys.exit(0)
