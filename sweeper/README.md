# Sweeper (Stealth Outflow)

This script automates the withdrawal of your profits from the 40 sniper wallets back to a Centralized Exchange (CEX).

**🚨 CRITICAL OPSEC RULE: DO NOT SEND ALL FUNDS TO THE SAME CEX DEPOSIT ADDRESS! 🚨**

If you send funds from 40 wallets back to a single Binance deposit address, your wallets will be instantly clustered on-chain. You will be permanently doxxed as the single entity controlling all 40 wallets.

## How to use this safely

1. **Generate 40 Unique Deposit Addresses.**
   - Binance allows standard users to generate up to 20 deposit addresses per network.
   - To get 40, you must use **Binance Sub-accounts** or use multiple exchanges (e.g., 20 on Binance, 20 on OKX/Bybit/Kraken).
   
2. **Create the mapping file.**
   - Rename `deposit_addresses.example.csv` to `deposit_addresses.csv`.
   - Put exactly **one unique deposit address per wallet index**.

3. **Install dependencies.**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment.**
   - Copy `.env.example` to `.env`.
   - Add your `HELIUS_API_KEY`.

5. **Run the Sweeper.**
   ```bash
   python sweep_to_cex.py
   ```

The script will read each wallet's balance, reserve a tiny amount for the transaction fee, and sweep the rest directly to its mapped CEX address. It waits 30-120 seconds between transactions to break temporal clustering algorithms.

### 🚨 Never reuse sniper wallets!
Once these 40 wallets are swept, **abandon them**. If you reuse the exact same 40 wallets for your next token launch, bots will notice that the exact same 40 wallets all bought Token A last month and Token B this month. That is "Behavioral Clustering". Always generate 40 fresh wallets for every new launch.
