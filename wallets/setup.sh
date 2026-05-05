#!/usr/bin/env bash
# Production setup script for Ubuntu 22.04+ / Debian / WSL Ubuntu.
# Installs Solana CLI (Agave) + Python deps and prepares the project layout.
#
# Usage:  bash setup.sh
set -euo pipefail

SOLANA_VERSION="${SOLANA_VERSION:-stable}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '\033[1;36m[setup]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[setup]\033[0m %s\n' "$*" >&2; }

# --- 1. apt prerequisites -------------------------------------------------
if command -v apt-get >/dev/null 2>&1; then
    log "installing apt prerequisites (curl, build-essential, pkg-config, libssl-dev, python3-venv, python3-pip)"
    sudo apt-get update -y
    sudo apt-get install -y --no-install-recommends \
        curl ca-certificates build-essential pkg-config libssl-dev \
        python3 python3-venv python3-pip
else
    err "apt-get not found. This script targets Ubuntu/Debian."
    exit 1
fi

# --- 2. Solana CLI (Agave) -----------------------------------------------
if command -v solana-keygen >/dev/null 2>&1; then
    log "solana-keygen already installed: $(solana-keygen --version)"
else
    log "installing Solana CLI (Agave) version=${SOLANA_VERSION}"
    sh -c "$(curl -sSfL https://release.anza.xyz/${SOLANA_VERSION}/install)"
    # The installer adds ~/.local/share/solana/install/active_release/bin to PATH
    # via shell profile; we export it for the current shell so the verify step
    # below works immediately.
    export PATH="${HOME}/.local/share/solana/install/active_release/bin:${PATH}"
    if ! command -v solana-keygen >/dev/null 2>&1; then
        err "solana-keygen still not on PATH after install."
        err "open a new shell or run: export PATH=\"\$HOME/.local/share/solana/install/active_release/bin:\$PATH\""
        exit 1
    fi
    log "solana-keygen installed: $(solana-keygen --version)"
fi

# --- 3. Python venv + deps -----------------------------------------------
cd "$PROJECT_DIR"
if [[ ! -d ".venv" ]]; then
    log "creating Python virtualenv at .venv"
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
log "upgrading pip and installing requirements.txt"
pip install --upgrade pip wheel >/dev/null
pip install -r requirements.txt

# --- 4. Project dirs + permissions ---------------------------------------
log "creating project directories"
mkdir -p private public logs
chmod 700 private || true

if [[ ! -f "proxies.txt" ]]; then
    log "no proxies.txt found - run:  cp proxies.txt.example proxies.txt  and edit it."
fi

log "setup complete."
log "next:"
log "  source .venv/bin/activate"
log "  python generate_wallets.py -n 40"
