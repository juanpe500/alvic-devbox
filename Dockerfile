# Alvic devbox — a private Linux workstation reachable only from your tailnet.
# Built and run by install.sh; see the comments there for the isolation model.
FROM ubuntu:24.04

LABEL org.opencontainers.image.source="https://github.com/juanpe500/alvic-devbox" \
      org.opencontainers.image.description="Alvic devbox: private, tailnet-only Linux workstation"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg git git-lfs openssh-client sudo \
        build-essential pkg-config python3 python3-venv python3-pip python3-dev \
        nodejs npm ripgrep jq tmux nano vim less unzip zip procps htop \
        iproute2 dnsutils file tree \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI (official apt repo).
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh && rm -rf /var/lib/apt/lists/*

# Tailscale (official installer detects Ubuntu and uses apt).
RUN curl -fsSL https://tailscale.com/install.sh | sh

# The human/agent account. Passwordless sudo inside the container only.
RUN useradd -m -s /bin/bash dev \
    && echo "dev ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/dev \
    && chmod 0440 /etc/sudoers.d/dev

# Control agent (FastAPI) in its own venv, owned by root, run as dev.
COPY agent/requirements.txt /opt/agent/requirements.txt
RUN python3 -m venv /opt/agent/.venv \
    && /opt/agent/.venv/bin/pip install --no-cache-dir -r /opt/agent/requirements.txt
COPY agent/agent.py /opt/agent/agent.py

USER dev
WORKDIR /home/dev
# Claude Code (native installer → ~/.local/bin/claude) and the default folders.
# /home/dev is a named volume: Docker seeds it from this layer on first run.
RUN curl -fsSL https://claude.ai/install.sh | bash \
    && mkdir -p ~/repos ~/scratch \
    && echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc \
    && git config --global init.defaultBranch main

USER root
COPY entrypoint.sh /usr/local/bin/devbox-entrypoint
RUN chmod +x /usr/local/bin/devbox-entrypoint

ENTRYPOINT ["/usr/local/bin/devbox-entrypoint"]
