# alvic-devbox

A private Linux workstation on your own VPS that you drive from your iPhone with
[Alvic](https://github.com/juanpe500). Clone repos, run a real shell, build and test,
push to GitHub, run CLIs, and run **Claude Code**, all from the phone.

It behaves like a personal laptop, not a server: nothing is published to the internet.
The only way in is your own [Tailscale](https://tailscale.com) network.

```
 iPhone (Alvic + Tailscale app)
        │  HTTPS, tailnet only
        ▼
 ┌──────────── your VPS ────────────────────────────────────────┐
 │  existing services (web apps, databases…)  ◄── blocked ──┐   │
 │                                                          │   │
 │  ┌── alvic-devbox container (2 CPU / 4 GB by default) ───┴─┐ │
 │  │  tailscale (userspace)  →  agent on 127.0.0.1:7777      │ │
 │  │  bash · tmux · git · gh · python · node · ripgrep       │ │
 │  │  Claude Code · ~/repos (persistent volume)              │ │
 │  └────────────────────────────────────────── internet ──►  │ │
 └──────────────────────────────────────────────────────────────┘
```

## What you get

| | |
|---|---|
| **Terminal** | A real interactive terminal in Alvic, backed by tmux. Sessions keep running when you leave the app, and reopening re-attaches. |
| **Claude Code** | Preinstalled. Run several named sessions in parallel from Alvic, with live status, Remote Control and resume. |
| **Browser** | A headless Chromium that Claude drives through MCP (navigate, click, type, read, screenshot, batch) to test what it builds. Watch any tab live in Alvic and take over with a tap. Tabs are pooled by RAM and saved (screenshot, URL, scroll) when a session finishes. |
| **Repos** | `~/repos` on a persistent volume. Clone, pull, commit and push with `git` / `gh`. |
| **Files** | Browse and edit any file from the phone. Deleting a non-empty folder always asks first. |
| **Jobs** | Background processes for dev servers, installs and long builds, with live logs. |
| **Previews** | Open any dev server port inside Alvic. It never leaves your tailnet. |
| **Your CLIs** | Anything that runs on Linux: `pip install` your own tools, deploy scripts, and so on. |

## Isolation model

Built to share a box with a running app (for example a QA server) without touching it.

- **No published ports.** The container listens only on `127.0.0.1` and is reached through
  `tailscale serve` (tailnet HTTPS). Funnel (public exposure) is never enabled.
- **Firewall.** Nothing new can enter the devbox network, and the devbox cannot open
  connections to the host itself or to any private or LAN range. Your other services and
  databases on the same machine are unreachable from inside. The one exception is the host's
  own SSH (port 22, still key-only), so deploy scripts can reach that box like any other
  server. Outbound internet (git, pip, npm, Claude, deploy CLIs) works.
- **Resource limits.** cgroup caps on CPU, memory and process count, so the host's own
  workloads keep their headroom.
- **No host access.** No Docker socket, no host mounts, no `--privileged`, no `NET_ADMIN`.
- **Token auth.** Every request to the agent needs a random token generated at install time.

## Requirements

- A Linux VPS with **Docker**, and root (or passwordless `sudo`).
- A **Tailscale** account (the free personal plan is enough) with **MagicDNS** and
  **HTTPS Certificates** enabled: admin console → DNS.
- A Tailscale **auth key**: admin console → Settings → Keys → Generate auth key.
- The **Tailscale app** on your iPhone, signed in to the same account.

## Install

On the VPS:

```bash
curl -fsSL https://raw.githubusercontent.com/juanpe500/alvic-devbox/main/install.sh \
  | sudo env TS_AUTHKEY=tskey-auth-XXXX bash
```

It pulls the prebuilt image (`ghcr.io/juanpe500/alvic-devbox`, amd64 and arm64), creates the
isolated network and firewall, starts the container, and prints:

```
================ Alvic devbox ready ================
URL:   https://alvic-devbox.tailXXXX.ts.net
Token: 3f9c…
```

In Alvic: **Apps → + → Full environment**, paste the URL and the token, then tap
**Test connection** and **Save**. The same steps, with a copy button, are in that screen
under *How to install*.

The first time, sign in from the devbox terminal:

```bash
gh auth login      # GitHub, for private repos and pushes
claude             # Claude Code
```

## Options

Pass these after `env` in the install command:

| Variable | Default | |
|---|---|---|
| `TS_AUTHKEY` | — | Required on first install only. |
| `DEVBOX_CPUS` | `2` | CPU limit. |
| `DEVBOX_MEMORY` | `4g` | Memory limit (swap is capped to the same value). |
| `DEVBOX_NAME` | `alvic-devbox` | Container name and tailnet hostname. |
| `DEVBOX_SUBNET` | `172.31.250.0/24` | Private Docker network. Change it if it clashes. |
| `DEVBOX_IMAGE` | GHCR image | Use your own image. |
| `DEVBOX_BUILD` | `0` | `1` builds from source instead of pulling. |

Example for a busy box: `sudo env TS_AUTHKEY=… DEVBOX_CPUS=3 DEVBOX_MEMORY=3g bash`.

## Update / uninstall

```bash
# update: same command, no auth key needed (repos, sessions and token are kept)
curl -fsSL https://raw.githubusercontent.com/juanpe500/alvic-devbox/main/install.sh | sudo bash

# uninstall: removes the container, network and firewall; keeps the data volumes
curl -fsSL https://raw.githubusercontent.com/juanpe500/alvic-devbox/main/install.sh | sudo bash -s uninstall
```

Data lives in the Docker volumes `alvic-devbox-home` (your home folder and repos) and
`alvic-devbox-tailscale` (the tailnet identity). Delete them with `docker volume rm` to start fresh.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Install says *First install needs TS_AUTHKEY* | Generate an auth key in the Tailscale admin console and pass it as shown above. |
| `tailscale serve failed` in `docker logs alvic-devbox` | Turn on **HTTPS Certificates** in the Tailscale admin console → DNS, then re-run the install. |
| SSL error from the phone (certificate still pending: `docker logs alvic-devbox` shows ACME errors) | Use `http://alvic-devbox.<tailnet>.ts.net` instead. It is still tailnet-only and encrypted by WireGuard, and needs no certificate. |
| Alvic can't reach the devbox | Make sure the Tailscale app on the phone is connected. Check `docker logs alvic-devbox`. |
| Lost the token | `sudo cat /etc/alvic-devbox/token` on the VPS. |
| A private repo won't clone | Run `gh auth login` in the devbox terminal first. |

## Repository layout

```
Dockerfile          Ubuntu 24.04 + git, gh, python, node, ripgrep, tmux, tailscale, Claude Code
entrypoint.sh       tailscale (userspace) + `tailscale serve` → agent
agent/agent.py      FastAPI agent: files, exec, jobs, ports, HTTP proxy, WebSocket PTY (tmux),
                    Claude Code session manager
agent/browser.py    shared headless Chromium: MCP tools, tab pool, save/restore, live view
install.sh          pull/build, network, firewall (systemd unit), container
tests/              smoke tests for the agent, Claude sessions and browser (run in CI before every build)
```
