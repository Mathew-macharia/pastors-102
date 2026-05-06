# Binance API Funder (Stealth Inflow)

This script automates the withdrawal of SOL directly from your Binance account to your 40 sniper wallets.

**Why use this instead of the old `funder/`?**
The old funder used a single Treasury wallet. To 2026 sniper bots, this instantly clusters your 40 wallets into a single entity and gets your token blacklisted as a Wash Trade / Rug Risk.
By withdrawing directly from Binance, the funds originate from Binance's massive "Hot Wallets". To any blockchain observer, your 40 wallets look completely unrelated, just like 40 random strangers.

## Important Note on the "1 SOL" Math

You stated you are investing **1 SOL in total** for all 40 wallets.
`1 SOL / 40 wallets = 0.025 SOL per wallet.`

Binance charges a withdrawal fee (typically ~0.005 to 0.008 SOL for Solana network).
If the script withdraws 0.025 SOL, the receiving wallet will get `~0.017 SOL`.

**The bot config (`bot/config.toml`) MUST be updated so `buy.min_sol` and `buy.max_sol` are lower than 0.015**, or the buys will fail due to insufficient funds (the bot needs ~0.0035 SOL left over for ATA rent, priority fees, and Jito tips). 

## Setup

1. `pip install -r requirements.txt` (or install in a `.venv`)
2. Copy `.env.example` to `.env`.
3. Create an API Key in your Binance account.
   * **Permissions:** Enable Reading & Enable Withdrawals.
   * **Security:** Ensure you restrict the API key to your current IP address, or Binance will automatically disable the withdrawal permission.
4. Add the API Key and Secret to the `.env` file.

## Usage

```bash
python withdraw.py
```

The script will:
- Read your 40 wallets from `../wallets/wallets.csv`.
- Randomize amounts (e.g., 0.024 to 0.026 SOL).
- Withdraw directly to each wallet.
- **Wait 2 to 15 minutes between each withdrawal.** This is critical to defeat temporal clustering (so you aren't flagged by exchanges or bots for bursting 40 withdrawals in the same second).

If the script crashes or you stop it, simply rerun it. It uses `state/funding_state.json` to remember which wallets have already been funded and will skip them.
