#!/usr/bin/env bash
# Bot setup script. Targets Ubuntu 22.04+ / WSL.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

log() { printf '\033[1;36m[bot-setup]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[bot-setup]\033[0m %s\n' "$*" >&2; }

# --- Rust toolchain ------------------------------------------------------
if ! command -v cargo >/dev/null 2>&1; then
    log "installing Rust toolchain (rustup)"
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
    # shellcheck disable=SC1091
    source "$HOME/.cargo/env"
else
    log "rust already installed: $(cargo --version)"
fi

# --- system deps for native crates ---------------------------------------
if command -v apt-get >/dev/null 2>&1; then
    log "installing apt prerequisites (build tools, openssl-dev for native crates)"
    sudo apt-get update -y
    sudo apt-get install -y --no-install-recommends \
        build-essential pkg-config libssl-dev clang
fi

# --- prereq files --------------------------------------------------------
if [[ ! -f "../wallets/wallets.csv" ]]; then
    err "wallets.csv not found at ../wallets/wallets.csv"
    err "Run ../wallets/generate_wallets.py first."
    exit 1
fi

# --- config + env --------------------------------------------------------
if [[ ! -f "config.toml" ]]; then
    cp config.example.toml config.toml
    log "config.toml created -- edit it (helius_api_key + triton_url)"
fi
if [[ ! -f ".env" ]]; then
    cp .env.example .env
    log ".env created -- edit it (HELIUS_API_KEY, TRITON_RPC_URL, PUMP_FEE_RECIPIENT)"
fi

# --- build ---------------------------------------------------------------
log "compiling release build (this takes a few minutes the first time)..."
cargo build --release

cat <<EOF

================================================================================
  pastors-bot built successfully.

  Next steps:
    1. Edit .env  -> set HELIUS_API_KEY, TRITON_RPC_URL, PUMP_FEE_RECIPIENT
    2. Edit config.toml -> tune buy/sell amounts, slippage, jitter, etc.
    3. Run:    ./target/release/pastors-bot --fee-recipient \$PUMP_FEE_RECIPIENT
    4. Open:   http://127.0.0.1:7777/

  In the UI:
    - Paste your token mint + creator wallet
    - Optionally pick a schedule time + timezone
    - Click "arm" -> then "FIRE NOW" or wait for the schedule
================================================================================
EOF
