# CEX funder — Option B (OKX + Bybit stealth inflow)

This folder automates **on-chain SOL withdrawals** from **two exchanges** into your 40 sniper wallets:

| Rows in `wallets.csv` (sorted by `index` ↑) | Exchange | Default count |
| --- | --- | --- |
| First N rows | **OKX** | 24 |
| Remaining rows | **Bybit** | 16 |

Totals are controlled with `OKX_WALLET_COUNT` + `BYBIT_WALLET_COUNT` in `.env` (must equal the number of wallet rows).

**Why two CEXs instead of one treasury wallet?**  
On-chain graph tools cluster on a single funding hub. Withdrawals sourced from unrelated major exchange hot-wallet pools (here: two different CEX ecosystems) reduce single-source fan-in compared to one treasury key paying everyone.

**Why not Binance-only for ~0.025 SOL each?**  
Binance’s published Solana minimum withdrawal is **0.1 SOL** per request, so forty micro-withdrawals from one SOL of capital are **not API-viable**. OKX and Bybit support much smaller Solana minimums; this script targets ~0.024–0.026 SOL notional per wallet so **~1 SOL** total still maps to 40 wallets.

## Prerequisites

1. **OKX** — API key with **Withdraw** permission; IP whitelist recommended. SOL must be available in the **Funding** account (transfer from Trading if needed).
2. **Bybit** — Master-account API key with **Withdraw**; IP whitelist. SOL must be in the **Funding** wallet (`FUND`). **Every destination pubkey must exist in Bybit’s withdrawal address book** (exact string match, case-sensitive) before running.
3. **`wallets/wallets.csv`** — exactly `OKX_WALLET_COUNT + BYBIT_WALLET_COUNT` rows (default 40).

## Setup

```bash
cd binance_funder
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env — real keys, optional split overrides
```

## Capital plan (~1 SOL notional)

Target per wallet: **0.024–0.026 SOL** requested (randomized). The exchange **adds network fees on top** of that from your CEX balance.

Rough split (defaults):

- **OKX** — fund ≈ **0.65 SOL** (covers 24 × ~0.025 + chain fees).
- **Bybit** — fund ≈ **0.45 SOL** (covers 16 × ~0.025 + chain fees).

Top up after checking each venue’s live fee table in the app.

## Run

```bash
python withdraw.py
```

Behavior:

- Reads `../wallets/wallets.csv`, sorts by numeric `index`.
- First `OKX_WALLET_COUNT` rows → OKX `POST /api/v5/asset/withdrawal` (chain + `fee` auto-loaded from public `GET /api/v5/asset/currencies?ccy=SOL`, overridable via `.env`).
- Remaining rows → Bybit `POST /v5/asset/withdraw/create` (`forceChain=1`, `accountType=FUND`).
- **Random delay 2–15 minutes** between any two withdrawals (temporal de-clustering).
- **Bybit**: enforces ≥ **11 seconds** between Bybit calls (API limit: one withdraw per 10 s per coin+chain).
- **Resume-safe** — `state/funding_state.json` records completed pubkeys; re-run skips them.

## Logs

- `logs/funding.log` — append-only UTC log.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| OKX `58203` / address errors | Add/whitelist address in OKX; some regions require address book first. |
| Bybit `retCode` non-zero | Add exact pubkey to **Withdrawal address book**; wait 10 s between manual tests. |
| `OKX_WALLET_COUNT + BYBIT_WALLET_COUNT must equal wallet rows` | Fix `.env` counts or regenerate `wallets.csv` to match. |
| OKX `58211` fee error | Set `OKX_SOL_FEE` in `.env` to the `minFee` shown in OKX UI for SOL-Solana. |
