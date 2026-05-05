# pastors-102-v2

Four-phase Solana operations stack: **wallet generation → direct funding →
swap-laundered multi-hop funding → Pump.fun sniper bot**.

Each phase is independent, has its own README + setup script, and is
wired together through filesystem hand-offs (CSV + JSON keypairs) — no
runtime IPC.

---

## Phases

| Phase | Folder        | Language | Purpose                                                                                                |
| ----- | ------------- | -------- | ------------------------------------------------------------------------------------------------------ |
| 1     | [`wallets/`](wallets/README.md)   | Python   | Generate 40 Solana keypairs via `solana-keygen`, randomized delays, sticky-session proxy tagging       |
| 2     | [`funder/`](funder/README.md)    | Python   | Fund the 40 wallets directly from a treasury via Helius Sender (SWQoS / dual-route) with priority fees |
| 3     | [`multihop/`](multihop/README.md)  | Python   | Alternative to phase 2: 5-stage swap-laundered pipeline (treasury → A → SOL→USDC → B → USDC→SOL → 40)  |
| 4     | [`bot/`](bot/README.md)       | Rust     | Pump.fun sniper bot: 8 atomic Jito bundles × 5 wallets, Helius+Triton RPC rotation, web UI trigger     |

---

## Data flow

```
                 ┌─────────────────────────────────────┐
                 │ Phase 1: wallets/                   │
                 │   generate_wallets.py               │
                 │   -> wallets/private/<pk>.json      │
                 │   -> wallets/wallets.csv            │
                 └──────────────┬──────────────────────┘
                                │
                ┌───────────────┴───────────────┐
                │                               │
                ▼                               ▼
   ┌────────────────────────┐     ┌─────────────────────────────────┐
   │ Phase 2: funder/       │     │ Phase 3: multihop/              │
   │   fund_wallets.py      │     │   multihop.py                   │
   │   reads wallets.csv    │ OR  │   reads wallets.csv +           │
   │   uses treasury        │     │   reuses funder/treasury/       │
   │   sends SOL direct     │     │   sends SOL through             │
   │                        │     │   16 fresh intermediates +      │
   │                        │     │   2 Jupiter swaps               │
   └───────────┬────────────┘     └────────────────┬────────────────┘
               │                                   │
               └───────────────────┬───────────────┘
                                   │
                                   ▼
                ┌─────────────────────────────────────┐
                │ Phase 4: bot/                       │
                │   pastors-bot (Rust)                │
                │   reads wallets/private/<pk>.json   │
                │   web UI on :7777                   │
                │   -> 8 Jito bundles to Pump.fun     │
                │   -> sell after N blocks            │
                └─────────────────────────────────────┘
```

You run **either** phase 2 (direct, faster, traceable) or phase 3
(multi-hop with swaps, slower, harder to cluster). Not both.

---

## Quick start (Ubuntu / WSL Ubuntu)

```bash
# 1. Generate the 40 wallets
cd wallets
bash setup.sh
source .venv/bin/activate
python generate_wallets.py
deactivate
cd ..

# 2a. Fund directly  -- OR  --  2b. Fund through swaps (pick one)
cd funder      &&  bash setup.sh && source .venv/bin/activate && python fund_wallets.py
# OR
cd multihop    &&  bash setup.sh && source .venv/bin/activate && python multihop.py

# 3. Build + run the sniper bot
cd ../bot
bash setup.sh
./target/release/pastors-bot --fee-recipient $PUMP_FEE_RECIPIENT
# Then open http://127.0.0.1:7777/
```

Each `setup.sh` is idempotent and prints exactly which env vars to fill in.

---

## Security policy

The `.gitignore` at the repo root and inside each phase folder
**unconditionally excludes**:

- `private/`, `treasury/`, `intermediates/` — all keypair material
- `wallets.csv`, `master_pubkeys.txt`, `proxies.txt` — wallet metadata
- `state/`, `logs/`, `*.log` — operational data
- `.env` (any `.env`, anywhere) — every credential file
- `target/` — Rust build artifacts

If you fork this repo, do **not** loosen these. The `.env.example`
templates are committed; the real `.env` files never are.

POSIX permissions (`chmod 600` on keys, `chmod 700` on key directories)
take effect only on a real Linux filesystem. On WSL, keep this project
under `~/...`, **not** `/mnt/c/...`.

---

## OPSEC note

Read `multihop/README.md` → "OPSEC reality" and `bot/README.md` →
"Honest limitations" before using this against a real CEX deposit or a
live token launch. Multi-hop with swaps does not defeat
Chainalysis-grade forensics; same-block sniping with 40 sybil wallets is
a textbook anti-bot signature on Pump.fun.

This repo is engineered to be transparent about what it does and does
not protect against.
