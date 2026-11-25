#!/bin/bash
# Alvic devbox installer — run as root on the VPS:
#
#   curl -fsSL https://raw.githubusercontent.com/juanpe500/alvic-devbox/main/install.sh \
#     | sudo env TS_AUTHKEY=tskey-auth-... bash                  # install / upgrade
#   curl -fsSL …/install.sh | sudo bash -s uninstall              # remove container + firewall (keeps volumes)
#
# Pulls the prebuilt image from GHCR; if that fails (or DEVBOX_BUILD=1) it builds from
# source — this folder when run from a checkout, otherwise the repo's tarball.
#
# Optional: DEVBOX_CPUS (default 2), DEVBOX_MEMORY (default 4g), DEVBOX_NAME (default alvic-devbox),
#           DEVBOX_SUBNET (default 172.31.250.0/24), DEVBOX_IMAGE (default the GHCR image).
#
# Isolation model — the devbox behaves like a personal laptop, not a server:
#   • No ports are published on the host. The only way in is the tailnet (tailscale serve,
#     HTTPS, your devices only). Funnel/public exposure is never enabled.
#   • Firewall: nothing NEW may enter the devbox network; the devbox may not open
#     connections to the host itself or to any private/LAN range (so it can't touch the
#     QA app, its DB, or other containers). Outbound internet (git, pip, npm, Claude,
#     viclix/argo deploys) is allowed.
#   • cgroup limits on CPU, memory and PIDs so the host's QA keeps its resources.
#   • No docker socket, no host mounts, no --privileged, no NET_ADMIN.
#
# Self-update: the installer also sets up a host-side watcher (systemd .path unit). When the
# devbox asks for an update (Alvic → "Update environment" → the agent drops a request file in
# its own home volume), the watcher re-runs this installer with the saved settings. The
# container never gets Docker access: it can only ask for the official image to be re-pulled.
set -euo pipefail

CONF_DIR="/etc/alvic-devbox"
# Settings from the previous install win over defaults, so updates keep your limits.
SAVED_NAME="" SAVED_CPUS="" SAVED_MEMORY="" SAVED_SUBNET="" SAVED_IMAGE=""
# shellcheck disable=SC1091
[ -f "$CONF_DIR/config" ] && . "$CONF_DIR/config"
NAME="${DEVBOX_NAME:-${SAVED_NAME:-alvic-devbox}}"
CPUS="${DEVBOX_CPUS:-${SAVED_CPUS:-2}}"
MEMORY="${DEVBOX_MEMORY:-${SAVED_MEMORY:-4g}}"
SUBNET="${DEVBOX_SUBNET:-${SAVED_SUBNET:-172.31.250.0/24}}"
NETWORK="${NAME}-net"
BRIDGE="alvicbr0"
FW_BIN="/usr/local/sbin/alvic-devbox-firewall"
FW_UNIT="/etc/systemd/system/alvic-devbox-firewall.service"
UPD_BIN="/usr/local/sbin/alvic-devbox-update"
UPD_UNIT="/etc/systemd/system/alvic-devbox-updater.service"
UPD_PATH="/etc/systemd/system/alvic-devbox-updater.path"
REPO="juanpe500/alvic-devbox"
IMAGE="${DEVBOX_IMAGE:-${SAVED_IMAGE:-ghcr.io/$REPO:latest}}"
# Piped through `curl | bash` there is no script folder; only trust $0 when it has a Dockerfile beside it.
HERE=""
if [ -f "${BASH_SOURCE[0]:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/Dockerfile" ]; then
    HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo bash install.sh)"; exit 1; }
command -v docker >/dev/null || { echo "docker is not installed on this host"; exit 1; }

if [ "${1:-}" = "uninstall" ]; then
    docker rm -f "$NAME" 2>/dev/null || true
    systemctl disable --now alvic-devbox-updater.path 2>/dev/null || true
    rm -f "$UPD_PATH" "$UPD_UNIT" "$UPD_BIN"
    systemctl disable --now alvic-devbox-firewall.service 2>/dev/null || true
    "$FW_BIN" remove 2>/dev/null || true
    rm -f "$FW_UNIT" "$FW_BIN"
    systemctl daemon-reload
    docker network rm "$NETWORK" 2>/dev/null || true
    echo "Removed. Volumes ${NAME}-home and ${NAME}-tailscale were kept (docker volume rm to delete)."
    exit 0
fi

mkdir -p "$CONF_DIR"
chmod 700 "$CONF_DIR"
if [ ! -s "$CONF_DIR/token" ]; then
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$CONF_DIR/token"
    chmod 600 "$CONF_DIR/token"
fi
TOKEN="$(cat "$CONF_DIR/token")"
cat > "$CONF_DIR/config" <<EOF
SAVED_NAME="$NAME"
SAVED_CPUS="$CPUS"
SAVED_MEMORY="$MEMORY"
SAVED_SUBNET="$SUBNET"
SAVED_IMAGE="$IMAGE"
EOF
chmod 600 "$CONF_DIR/config"

first_install=1
docker volume inspect "${NAME}-tailscale" >/dev/null 2>&1 && first_install=0
if [ "$first_install" -eq 1 ] && [ -z "${TS_AUTHKEY:-}" ]; then
    echo "First install needs TS_AUTHKEY (Tailscale admin → Settings → Keys → Generate auth key)."
    exit 1
fi

LOCAL_TAG="$NAME:latest"
if [ "${DEVBOX_BUILD:-0}" != "1" ] && echo "==> Pulling $IMAGE" && docker pull "$IMAGE"; then
    docker tag "$IMAGE" "$LOCAL_TAG"
else
    echo "==> Building image from source (a few minutes)"
    SRC="$HERE"
    if [ -z "$SRC" ]; then
        SRC="$(mktemp -d)"
        trap 'rm -rf "$SRC"' EXIT
        curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/main" | tar -xz -C "$SRC" --strip-components=1
    fi
    docker build -t "$LOCAL_TAG" "$SRC"
fi

echo "==> Network $NETWORK ($SUBNET, bridge $BRIDGE, no inter-container traffic)"
if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
    docker network create --driver bridge --subnet "$SUBNET" \
        -o "com.docker.network.bridge.name=$BRIDGE" \
        -o "com.docker.network.bridge.enable_icc=false" \
        "$NETWORK" >/dev/null
fi

echo "==> Firewall"
cat > "$FW_BIN" <<EOF
#!/bin/bash
# Generated by alvic devbox install.sh — re-applied on every boot / docker restart.
set -e
BR="$BRIDGE"
remove() {
    iptables -D DOCKER-USER -j ALVIC-DEVBOX 2>/dev/null || true
    iptables -D INPUT -j ALVIC-DEVBOX-IN 2>/dev/null || true
    iptables -F ALVIC-DEVBOX 2>/dev/null || true; iptables -X ALVIC-DEVBOX 2>/dev/null || true
    iptables -F ALVIC-DEVBOX-IN 2>/dev/null || true; iptables -X ALVIC-DEVBOX-IN 2>/dev/null || true
}
if [ "\${1:-}" = "remove" ]; then remove; exit 0; fi
remove
iptables -N DOCKER-USER 2>/dev/null || true

# Forwarded traffic (devbox <-> anything that isn't the host itself).
iptables -N ALVIC-DEVBOX
iptables -A ALVIC-DEVBOX -o "\$BR" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -A ALVIC-DEVBOX -o "\$BR" -j DROP
for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10; do
    iptables -A ALVIC-DEVBOX -i "\$BR" -d "\$net" -j DROP
done
iptables -I DOCKER-USER 1 -j ALVIC-DEVBOX

# Traffic addressed to the host (QA web server, its DB, ssh…): replies only.
iptables -N ALVIC-DEVBOX-IN
iptables -A ALVIC-DEVBOX-IN -i "\$BR" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A ALVIC-DEVBOX-IN -i "\$BR" -j DROP
iptables -I INPUT 1 -j ALVIC-DEVBOX-IN
EOF
chmod 755 "$FW_BIN"

cat > "$FW_UNIT" <<EOF
[Unit]
Description=Alvic devbox isolation firewall
After=docker.service
Requires=docker.service
PartOf=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$FW_BIN
ExecStop=$FW_BIN remove

[Install]
WantedBy=multi-user.target docker.service
EOF
systemctl daemon-reload
systemctl enable alvic-devbox-firewall.service >/dev/null
systemctl restart alvic-devbox-firewall.service

echo "==> Container $NAME (cpus=$CPUS memory=$MEMORY)"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --hostname "$NAME" \
    --network "$NETWORK" \
    --dns 1.1.1.1 --dns 8.8.8.8 \
    --cpus "$CPUS" --memory "$MEMORY" --memory-swap "$MEMORY" --pids-limit 2048 \
    --cap-drop NET_RAW --cap-drop NET_ADMIN --cap-drop SYS_ADMIN \
    --restart unless-stopped \
    -v "${NAME}-home:/home/dev" \
    -v "${NAME}-tailscale:/var/lib/tailscale" \
    -e AGENT_TOKEN="$TOKEN" \
    -e TS_HOSTNAME="$NAME" \
    -e TS_AUTHKEY="${TS_AUTHKEY:-}" \
    "$NAME:latest" >/dev/null

echo "==> Self-update watcher"
VOL="$(docker volume inspect -f '{{.Mountpoint}}' "${NAME}-home" 2>/dev/null || true)"
if [ -n "$VOL" ]; then
    cat > "$UPD_BIN" <<EOF
#!/bin/bash
# Generated by alvic devbox install.sh. Runs when the devbox drops a request file in its
# home volume (Alvic → Update environment). Re-runs the official installer with the saved
# settings and leaves the result where the agent can read it.
REQ="$VOL/.devbox/update-request"
STATUS="$VOL/.devbox/update-status.json"
LOG=/var/log/alvic-devbox-update.log
[ -f "\$REQ" ] || exit 0
rm -f "\$REQ"
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
printf '{"state":"running","started_at":"%s"}\n' "\$(now)" > "\$STATUS"
if curl -fsSL https://raw.githubusercontent.com/$REPO/main/install.sh | bash > "\$LOG" 2>&1; then
    printf '{"state":"ok","finished_at":"%s"}\n' "\$(now)" > "\$STATUS"
else
    tail -c 1500 "\$LOG" | python3 -c 'import json,sys,datetime; print(json.dumps({"state":"failed","finished_at":datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),"log":sys.stdin.read()}))' > "\$STATUS" 2>/dev/null \
        || printf '{"state":"failed","finished_at":"%s"}\n' "\$(now)" > "\$STATUS"
fi
chmod 644 "\$STATUS"
EOF
    chmod 755 "$UPD_BIN"
    cat > "$UPD_UNIT" <<EOF
[Unit]
Description=Alvic devbox self-update (requested from the devbox)

[Service]
Type=oneshot
ExecStart=$UPD_BIN
EOF
    cat > "$UPD_PATH" <<EOF
[Unit]
Description=Watch for Alvic devbox update requests

[Path]
PathExists=$VOL/.devbox/update-request
Unit=alvic-devbox-updater.service

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable alvic-devbox-updater.path >/dev/null 2>&1 || true
    systemctl restart alvic-devbox-updater.path || true
fi

echo "==> Waiting for tailscale"
URL=""
for _ in $(seq 1 60); do
    DNS="$(docker exec "$NAME" sh -c 'tailscale status --json | jq -r .Self.DNSName' 2>/dev/null | sed 's/\.$//' || true)"
    [ "$DNS" = "null" ] && DNS=""
    if [ -n "$DNS" ]; then URL="https://$DNS"; break; fi
    sleep 2
done

echo
echo "================ Alvic devbox ready ================"
echo "URL:   ${URL:-<not up yet — check: docker logs $NAME>}"
echo "Token: $TOKEN"
echo
echo "In Alvic: Apps → + → Full environment → paste URL and token."
echo "Your phone must be on the same tailnet (Tailscale app, logged in)."
echo "Then, in the devbox terminal: gh auth login  ·  claude"
echo "===================================================="
