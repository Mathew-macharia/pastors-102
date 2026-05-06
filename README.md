# pastors-102-v2

A complete, production-ready Solana operations stack designed for launching a token on Pump.fun and sniping it with 40 automated wallets. 

Built with **stealth and clustering-immunity** in mind to defeat 2026 AI analyst bots (like Sybil Shield, Solsniffer) and human investigators.

---

## The 4-Phase Stealth Pipeline

To successfully evade on-chain clustering and wash-trade detection, you must follow this exact sequence:

1.  **`wallets/` (Generation)**
    *   Generates 40 fresh Solana keypairs locally.
2.  **`binance_funder/` (Stealth Inflow)**
    *   Withdraws SOL *directly* from Binance to each of your 40 wallets using the Binance API. 
    *   *Why?* It completely breaks the "Hub-and-Spoke" cluster model. To the blockchain, your wallets look like 40 random Binance users.
3.  **`bot/` (The Sniper)**
    *   High-speed Rust bot that uses **8 atomic Jito bundles** to make all 40 wallets buy your token in the same slot.
    *   Sells `N` blocks later using a **dual-path simultaneous fire** (Jito + Helius Sender) to guarantee rapid exit.
4.  **`sweeper/` (Stealth Outflow)**
    *   Sends the profits from your 40 wallets back to **40 UNIQUE CEX deposit addresses**.
    *   *Why?* If you send the profits back to a single deposit address, you instantly doxx your cluster.

### 🚨 DEPRECATED PHASES (DO NOT USE)
The folders `funder/` and `multihop/` are deprecated. They were built for an older standard of stealth. If you use them, 2026 sniper bots will flag your 40 wallets as a Wash Trade / Rug Risk because they trace all funding back to a single Treasury wallet. Use `binance_funder/` instead.

---

## 🚨 OPSEC: The "Burner Wallet" Rule
**Never reuse these 40 wallets for a second token launch.** If you reuse them, sniper bots will see that the exact same 40 wallets bought Token A last month and Token B this month. This triggers Behavioral Clustering algorithms and destroys your stealth.
**Rule:** One token launch = 40 fresh wallets. When the launch is done, use `sweeper/` to withdraw profits and throw the wallets away.

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

# 2. Fund them from Binance directly
cd binance_funder
# pip install -r requirements.txt (set up env, see README)
python withdraw.py
cd ..

# 3. Build + run the sniper bot
cd bot
bash setup.sh
./target/release/pastors-bot --fee-recipient $PUMP_FEE_RECIPIENT
# Then open http://127.0.0.1:7777/

# 4. Sweep the profits out
cd ../sweeper
# Map deposit_addresses.csv first (see README)
python sweep_to_cex.py
```

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
