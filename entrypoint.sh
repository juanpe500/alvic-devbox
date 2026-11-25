#!/bin/bash
# Starts tailscale in userspace mode (no NET_ADMIN, no TUN), publishes the agent
# to the tailnet ONLY via `tailscale serve` (never funnel), then runs the agent
# as `dev` bound to 127.0.0.1. Nothing in this container listens on the host or
# the public internet.
set -euo pipefail

: "${AGENT_TOKEN:?AGENT_TOKEN is required}"
TS_HOSTNAME="${TS_HOSTNAME:-alvic-devbox}"

mkdir -p /var/lib/tailscale /var/run/tailscale
tailscaled --tun=userspace-networking \
    --state=/var/lib/tailscale/tailscaled.state \
    --socket=/var/run/tailscale/tailscaled.sock &

for _ in $(seq 1 30); do
    [ -S /var/run/tailscale/tailscaled.sock ] && break
    sleep 1
done

if ! tailscale status >/dev/null 2>&1; then
    if [ -z "${TS_AUTHKEY:-}" ]; then
        echo "devbox: not logged in to tailscale and no TS_AUTHKEY given" >&2
        exit 1
    fi
    tailscale up --authkey="$TS_AUTHKEY" --hostname="$TS_HOSTNAME"
fi

# Tailnet-only HTTPS → agent. Requires MagicDNS + HTTPS certificates enabled in
# the tailnet admin console. Funnel (public exposure) is deliberately never used.
tailscale funnel reset >/dev/null 2>&1 || true
if ! tailscale serve --bg 7777; then
    echo "devbox: 'tailscale serve' failed — enable HTTPS certificates in the tailnet admin console" >&2
fi
# Plain HTTP too, still tailnet-only (WireGuard already encrypts end to end). Lets the phone
# connect even while the Let's Encrypt certificate for the HTTPS name is pending or failing.
tailscale serve --bg --http=80 7777 || true

unset TS_AUTHKEY

exec runuser -u dev -- env -i \
    HOME=/home/dev USER=dev SHELL=/bin/bash LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    PATH="/home/dev/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    AGENT_TOKEN="$AGENT_TOKEN" PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    /opt/agent/.venv/bin/uvicorn agent:app --app-dir /opt/agent \
        --host 127.0.0.1 --port 7777 --ws-ping-interval 20
