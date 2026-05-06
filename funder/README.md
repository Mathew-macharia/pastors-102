# [DEPRECATED] Solana Batch Funder (Phase 2)

**🚨 WARNING: DO NOT USE THIS SCRIPT IF YOU WANT TO AVOID ON-CHAIN CLUSTERING. 🚨**

This script funds 40 wallets from a single Treasury wallet. To modern 2026 sniper bots and analytics tools (like Sybil Shield or Bubblemaps), this immediately links all 40 wallets into a single "Entity". High-tier sniper bots will see this cluster buy a token, flag it as a Wash Trade / Rug Risk, and refuse to buy.

**To achieve true stealth, use `binance_funder/` instead.** It withdraws directly from Binance to your 40 wallets, making them look like 40 unrelated strangers.

---

## Original Readme Below (For reference only)

Fund the 40 wallets produced by [phase 1](../wallets/) from a single treasury,
using **Helius Sender** for ultra-low-latency transaction landing via SWQoS,
with randomized amounts, delays, priority fees, and tip accounts.

---

## Pipeline

```
Binance (your account)
        |
        | (1) one-time withdrawal: ~5 SOL
        v
  Treasury Wallet (treasury/treasury.json)
        |
        | (2) 40 separate funding txs
        | - random 0.09 - 0.11 SOL per wallet
        | - random 30 - 300s delay between txs
        | - random priority fee (jittered around Helius estimate)
        | - random tip account (1 of 10) + random tip amount
        | - sent via https://sender.helius-rpc.com/fast?swqos_only=true
        v
  40 wallets (../wallets/wallets.csv)
```

---

## Layout

```
funder/
├── fund_wallets.py        # main script
├── setup.sh               # one-shot installer (Python deps + treasury keypair)
├── requirements.txt       # solders, requests, python-dotenv
├── .env.example           # template -> copy to .env, set HELIUS_API_KEY
├── .gitignore             # excludes treasury/, state/, logs/, .env
├── README.md
│
├── treasury/              # chmod 700.  NEVER commit.
│   └── treasury.json      # 64-byte keypair (chmod 600). Receives the Binance withdrawal.
│
├── logs/
│   └── funding.log        # UTC per-tx log
│
└── state/
    └── funding_state.json # resumable state: pending|sent|confirmed|failed per wallet
```

---

## Why Helius Sender + SWQoS-only

- **Sender** dual-routes transactions to validators and Jito simultaneously
  for sub-second landing. Free on all plans, no API credits consumed,
  default 50 TPS.
- **SWQoS-only** mode (`?swqos_only=true`) routes exclusively through
  Stake-Weighted QoS infrastructure. Same speed for non-MEV traffic, but
  the **tip floor drops from 0.0002 SOL to 0.000005 SOL** — ~40× cheaper
  per tx. For 40 internal funding transfers (no MEV concern), this saves
  ~0.0078 SOL.
- The official Solana research consensus (Chorus One, 2025-2026) is that
  **SWQoS dominates priority fees and Jito tips for landing latency**.
  Priority fees mostly determine *block ordering*, not whether you land
  next slot.

If you ever need MEV-tier inclusion (e.g. swaps), pass `--dual` to switch
to dual-route Sender at the higher 0.0002 SOL minimum tip.

---

## Honest OPSEC note (read this carefully)

The randomization in this script (delays, amounts, priority fees, tip
accounts) **does not defeat on-chain clustering**. Anyone with a Solana
explorer (Solscan, SolanaFM) can open the treasury address and immediately
see all 40 outgoing transfers and their destinations. Random amounts and
delays only fool naïve automated bots — they do **not** stop a human
analyst, an exchange compliance team, or a chain analytics firm.

**What this script actually buys you:**

- Transactions look human-paced rather than scripted-burst (delay jitter)
- Amounts are not literally identical (amount jitter)
- The treasury → 40-wallet fan-out *exists* but is harder for naïve
  pattern matchers to flag as a "sybil cluster"
- Each funding tx lands in 1-2 slots via SWQoS

**What this script does NOT buy you:**

- Clustering immunity. Treasury → 40 wallets is permanently linked on
  chain. Visible to anyone, forever.
- Anonymity from a determined investigator
- CEX-level "fresh wallet" appearance

**If clustering immunity is the actual goal**, you need either:

1. **Per-wallet CEX withdrawals**: 40 separate withdrawals from Binance
   directly to each of the 40 wallets (over days/weeks, with delays).
   No on-chain link to a single treasury.
2. **Multi-hop with swaps**: treasury → intermediate wallets → swap on
   Jupiter via a different mint (e.g. SOL → USDC → SOL with different
   intermediate addresses) → final wallets. Adds friction but breaks
   simple `traceOutflows` analysis.
3. **A privacy primitive on Solana** (limited options today; most have
   regulatory/withdrawal frictions).

This script intentionally implements only what you asked for. It does
**not** silently add multi-hop routing.

---

## Install — Ubuntu / WSL Ubuntu

From inside `~/pastors-102-v2/funder` (after migrating from `/mnt/c/...`):

```bash
bash setup.sh
```

`setup.sh` is idempotent and:

1. Verifies `solana-keygen` is on PATH (reuses phase 1 install).
2. Creates `.venv/`, installs `requirements.txt`.
3. Creates `treasury/`, `state/`, `logs/` (chmod 700 on `treasury/`).
4. Generates the treasury keypair if one doesn't exist (chmod 600 on the file).
5. Prints the treasury address you'll send Binance funds to.
6. Copies `.env.example` to `.env` for you to fill in.

> **Back up the treasury seed phrase that `solana-keygen new` prints.**
> If you lose `treasury/treasury.json` AND don't have the seed phrase,
> any SOL still in that wallet is gone forever.

---

## Step-by-step: Binance USDT/SOL → Treasury

You said you have USDT/crypto on Binance. The clean path is:

### Option A — convert USDT → SOL on Binance, withdraw SOL (recommended)

1. **Binance → Convert** (or Spot Trade): convert your USDT to **SOL**.
   For 40 × 0.11 SOL max + tips + fees + 0.5 SOL safety buffer ≈ **5 SOL**.
   At ~$150/SOL that's ~$750 USDT. Withdraw a bit more (e.g. 5.2 SOL) so
   you can run again if anything fails.
2. **Binance → Wallet → Withdraw → SOL**.
3. **Network**: choose **Solana** (`SOL`). NOT BSC, NOT ETH.
4. **Address**: paste the treasury address that `setup.sh` printed (also
   visible via `solana-keygen pubkey treasury/treasury.json`).
5. **Amount**: at least 5 SOL.
6. **Submit**. Pass 2FA / email confirmation.
7. **Wait ~30 seconds**. Verify arrival:
   ```bash
   solana balance "$(solana-keygen pubkey treasury/treasury.json)" \
       --url https://api.mainnet-beta.solana.com
   ```
   You should see ~5 SOL minus Binance's withdrawal fee (currently
   ~0.000005 SOL on Solana, sometimes 0.01 SOL — Binance posts the exact
   fee at withdrawal time).

### Option B — withdraw USDT (SPL) on Solana, swap on-chain

Cheaper Binance withdrawal but adds an on-chain swap step on the treasury
side (extra surface area, extra fee, extra slippage). Skip unless you have
a strong reason. If you go this route:

1. Binance: withdraw **USDT** on **Solana** network to the treasury address.
2. Use Jupiter (https://jup.ag) to swap USDT → SOL on the treasury wallet.
3. Then run the funder normally.

I do not recommend Option B for this use case. Stick with Option A.

---

## Step-by-step: Helius API key

1. Sign up at https://dashboard.helius.dev/ (free).
2. Dashboard → API Keys → copy your key.
3. Edit `.env`:
   ```
   HELIUS_API_KEY=your_key_here_no_quotes
   ```
4. Sender works on the free tier with no API credits consumed.

---

## Run

```bash
source .venv/bin/activate

# Pre-flight only (checks balance, RPC, keys -- does NOT send anything)
python fund_wallets.py --dry-run

# Test on first 2 wallets only
python fund_wallets.py -n 2

# Full run (40 wallets)
python fund_wallets.py
```

**Approximate runtime**: 40 wallets × avg ~165 s delay = ~110 minutes.
Plus ~2-5 s per tx for build + send + confirm.

The script is **fully resumable**:

- `state/funding_state.json` records every wallet (`pending` →
  `sent` → `confirmed` / `failed`).
- A re-run skips already-confirmed wallets and retries failed ones.
- If a previous run crashed while a tx was in-flight (status=`sent`),
  the next run polls that txid first instead of building a duplicate
  transaction → no double-spending.

---

## Cost breakdown (per tx, with defaults)

| Component                       | Lamports                         | SOL                |
| ------------------------------- | -------------------------------- | ------------------ |
| Base fee (1 sig × 5000 L/sig)   | 5,000                            | 0.000005           |
| Priority fee (1500 CU × 200k μL) | up to 300                       | 0.0000003          |
| Sender tip (SWQoS-only min)     | 5,000 - 7,500 (random jitter)    | 0.000005 - 0.0000075 |
| Recipient transfer              | 90,000,000 - 110,000,000         | 0.09 - 0.11        |
| **Total per tx**                | ~90,010,300 - 110,015,000        | ~0.09 - 0.11       |
| **Total for 40 txs**            | ~3.6 - 4.4 SOL                   |                    |
| Overhead beyond recipient amount| ~0.0004 SOL across 40 txs        |                    |

The funder's overhead is < $0.10 total at typical SOL prices. The recipient
amount is what dominates (by design).

---

## CLI flags

```
python fund_wallets.py --help
```

| Flag                  | Default  | Notes                                          |
| --------------------- | -------- | ---------------------------------------------- |
| `--min-amount-sol`    | `0.09`   |                                                |
| `--max-amount-sol`    | `0.11`   |                                                |
| `--min-delay`         | `30`     | seconds                                        |
| `--max-delay`         | `300`    | seconds                                        |
| `--prio-min`          | `50000`  | microLamports per CU                           |
| `--prio-max`          | `200000` | microLamports per CU                           |
| `--dual`              | off      | Use full Sender (Jito + validators), 0.0002 SOL min tip |
| `--dry-run`           | off      | Pre-flight only                                |
| `-n N`, `--limit N`   | `0`      | Only fund the first N wallets (testing)        |

---

## Verifying after a run

```bash
# total funded across all 40 wallets
python -c "
import csv
total = 0
with open('../wallets/wallets.csv') as f:
    for r in csv.DictReader(f):
        # query each one's on-chain balance via solana CLI if you wanted
        print(r['pubkey'])
"

# or pick one and check on-chain
solana balance <PUBKEY_FROM_WALLETS_CSV> --url https://api.mainnet-beta.solana.com

# inspect a tx on Solscan
echo "https://solscan.io/tx/<TXID_FROM_state/funding_state.json>"
```

---

## Troubleshooting

| Symptom                                              | Cause / Fix                                                                 |
| ---------------------------------------------------- | --------------------------------------------------------------------------- |
| `ERROR: HELIUS_API_KEY not set`                      | Edit `.env`, set the key.                                                   |
| `ERROR: treasury underfunded`                        | Top up the treasury from Binance.                                           |
| `sender: Transaction must include a tip transfer...` | You modified the script and broke the tip-instruction. Sender requires it.  |
| Many `confirmation_timeout` failures                 | Network congestion. Bump `--prio-max` (e.g. `--prio-max 500000`). Re-run.   |
| `Pubkey.from_string` errors                          | Bad pubkey in `wallets.csv`. Check phase 1 output.                          |
| `state file treasury mismatch`                       | You re-generated the treasury. Delete `state/funding_state.json` to start fresh. |

---

## Security checklist before going live

- [ ] Project lives at `~/pastors-102-v2/funder`, NOT `/mnt/c/...`
      (so `chmod 600` actually takes effect).
- [ ] `treasury/treasury.json` is `chmod 600`.
- [ ] Treasury seed phrase is backed up offline (paper or hardware-encrypted USB).
- [ ] `.env` contains a valid `HELIUS_API_KEY` and is `chmod 600`.
- [ ] You ran `--dry-run` once before the real run.
- [ ] You ran `-n 2` on a tiny test before going to 40.
- [ ] You understand the OPSEC limits above.
