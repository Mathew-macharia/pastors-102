#!/usr/bin/env bash
# Multi-hop funder setup. Idempotent. Targets Ubuntu 22.04+ / WSL.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

log() { printf '\033[1;36m[multihop-setup]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[multihop-setup]\033[0m %s\n' "$*" >&2; }

if ! command -v solana-keygen >/dev/null 2>&1; then
    err "solana-keygen not found. Run ../wallets/setup.sh first."
    exit 1
fi

if [[ ! -f "../funder/treasury/treasury.json" ]]; then
    err "treasury keypair not found at ../funder/treasury/treasury.json"
    err "Run ../funder/setup.sh first to create the treasury wallet."
    exit 1
fi

if [[ ! -f "../wallets/wallets.csv" ]]; then
    err "wallets.csv not found at ../wallets/wallets.csv"
    err "Run ../wallets/generate_wallets.py first to create the 40 final wallets."
    exit 1
fi

if [[ ! -d ".venv" ]]; then
    log "creating .venv"
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
log "installing requirements.txt"
pip install --upgrade pip wheel >/dev/null
pip install -r requirements.txt

mkdir -p intermediates state logs
chmod 700 intermediates || true

if [[ ! -f ".env" ]]; then
    cp .env.example .env
    log ".env created from template -- set HELIUS_API_KEY"
fi

cat <<EOF

================================================================================
  Multi-hop funder ready.

  Next steps:
    1. Edit .env  ->  set HELIUS_API_KEY  (or copy from ../funder/.env)
    2. Make sure your treasury has at least ~6 SOL  (50% buffer over the
       direct-funder amount because of swap slippage and ATA rents).
    3. Dry-run first:
         source .venv/bin/activate
         python multihop.py --dry-run
    4. Run a single stage to test:
         python multihop.py --only-stage 1
    5. Full run (all 5 stages, ~90 min runtime):
         python multihop.py
================================================================================
EOF
