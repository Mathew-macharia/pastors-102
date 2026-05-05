#!/usr/bin/env bash
# Phase 2 setup. Idempotent. Targets Ubuntu 22.04+ / WSL Ubuntu.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

log() { printf '\033[1;36m[funder-setup]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[funder-setup]\033[0m %s\n' "$*" >&2; }

# --- 1. solana-keygen (reuses phase 1 install if present) ---------------
if ! command -v solana-keygen >/dev/null 2>&1; then
    err "solana-keygen not found. Run ../wallets/setup.sh first or install:"
    err "  sh -c \"\$(curl -sSfL https://release.anza.xyz/stable/install)\""
    exit 1
fi

# --- 2. Python venv + deps ----------------------------------------------
if [[ ! -d ".venv" ]]; then
    log "creating .venv"
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
log "installing requirements.txt"
pip install --upgrade pip wheel >/dev/null
pip install -r requirements.txt

# --- 3. Project dirs + permissions --------------------------------------
log "creating project directories"
mkdir -p treasury logs state
chmod 700 treasury || true

# --- 4. Treasury keypair -------------------------------------------------
if [[ ! -f "treasury/treasury.json" ]]; then
    log "generating new treasury keypair (12-word seed phrase will print -- back it up offline)"
    solana-keygen new --no-bip39-passphrase --outfile treasury/treasury.json
    chmod 600 treasury/treasury.json || true
else
    log "treasury keypair already exists at treasury/treasury.json"
fi

TREASURY_PUBKEY="$(solana-keygen pubkey treasury/treasury.json)"
log "treasury address: $TREASURY_PUBKEY"

# --- 5. .env --------------------------------------------------------------
if [[ ! -f ".env" ]]; then
    cp .env.example .env
    log ".env created from template -- edit it and set HELIUS_API_KEY"
fi

cat <<EOF

================================================================================
  Treasury wallet ready.

  Address (send SOL here from Binance):
      $TREASURY_PUBKEY

  Next steps:
    1. Edit .env  -> set HELIUS_API_KEY (free at https://dashboard.helius.dev/)
    2. Withdraw at least 5 SOL from Binance to the address above (Network: Solana)
    3. Verify arrival:    solana balance $TREASURY_PUBKEY --url https://api.mainnet-beta.solana.com
    4. Dry-run first:     source .venv/bin/activate && python fund_wallets.py --dry-run
    5. Real run:          source .venv/bin/activate && python fund_wallets.py
================================================================================
EOF
