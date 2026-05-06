import csv
import json
import os
import time
import random
import sys
from datetime import datetime
from binance.client import Client
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')

# Paths
WALLETS_CSV = '../wallets/wallets.csv'
STATE_DIR = 'state'
STATE_FILE = f'{STATE_DIR}/funding_state.json'
LOG_DIR = 'logs'
LOG_FILE = f'{LOG_DIR}/funding.log'

# 1 SOL Total Budget Math (for 40 wallets)
# Binance withdrawal minimum for SOL is typically 0.015 - 0.02 SOL.
# The fee is usually ~0.005 - 0.008 SOL.
# If we withdraw 0.025 SOL, the wallet receives ~0.017 SOL. 
# Total withdrawn: 40 * 0.025 = 1 SOL.
MIN_WITHDRAW_SOL = 0.024
MAX_WITHDRAW_SOL = 0.026

# Delay to break temporal clustering (2 to 15 minutes between withdrawals)
MIN_DELAY_SEC = 120
MAX_DELAY_SEC = 900

def log(msg: str):
    timestamp = datetime.utcnow().isoformat() + "Z"
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_FILE, 'a') as f:
        f.write(line + "\n")

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_state(state: dict):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def load_wallets() -> list:
    wallets = []
    if not os.path.exists(WALLETS_CSV):
        print(f"ERROR: Wallets file {WALLETS_CSV} not found.")
        sys.exit(1)
        
    with open(WALLETS_CSV, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            wallets.append({
                'index': row['index'],
                'pubkey': row['pubkey']
            })
    return wallets

def main():
    if not API_KEY or not API_SECRET:
        print("ERROR: BINANCE_API_KEY and BINANCE_API_SECRET must be set in .env")
        sys.exit(1)

    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    log("Initializing Binance client...")
    client = Client(API_KEY, API_SECRET)

    # Check connection and withdrawal status
    try:
        details = client.get_asset_details(asset='SOL')
        if not details or not details.get('withdrawStatus'):
            log("WARNING: Cannot verify SOL withdrawal status, or withdrawals are suspended.")
    except Exception as e:
        log(f"WARNING: Could not fetch asset details: {e}")

    wallets = load_wallets()
    state = load_state()
    
    total_wallets = len(wallets)
    log(f"Loaded {total_wallets} wallets.")

    for i, w in enumerate(wallets):
        pubkey = w['pubkey']
        w_index = w['index']
        
        # Resume logic
        if pubkey in state and state[pubkey].get('status') == 'withdrawn':
            log(f"[{i+1}/{total_wallets}] Wallet {w_index} ({pubkey}) already funded. Skipping.")
            continue

        # Randomize amount
        amount = round(random.uniform(MIN_WITHDRAW_SOL, MAX_WITHDRAW_SOL), 4)
        
        log(f"[{i+1}/{total_wallets}] Initiating withdrawal of {amount} SOL to wallet {w_index} ({pubkey})...")
        
        try:
            # Binance withdrawal API call
            # network='SOL' ensures it goes over the Solana network
            result = client.withdraw(
                coin='SOL',
                address=pubkey,
                amount=amount,
                network='SOL',
                name=f"Sniper_Wallet_{w_index}"
            )
            
            withdraw_id = result.get('id', 'unknown')
            log(f"  -> Success! Withdrawal ID: {withdraw_id}")
            
            # Update state
            state[pubkey] = {
                'index': w_index,
                'amount': amount,
                'status': 'withdrawn',
                'withdraw_id': withdraw_id,
                'timestamp': datetime.utcnow().isoformat() + "Z"
            }
            save_state(state)
            
        except BinanceAPIException as e:
            log(f"  -> ERROR from Binance API: {e}")
            log("Aborting to prevent partial or broken state. Fix the error and re-run.")
            sys.exit(1)
        except Exception as e:
            log(f"  -> ERROR: {e}")
            log("Aborting. Fix the error and re-run.")
            sys.exit(1)
            
        # If not the last wallet, wait to avoid clustering
        if i < total_wallets - 1:
            delay = random.randint(MIN_DELAY_SEC, MAX_DELAY_SEC)
            log(f"Sleeping for {delay} seconds to avoid temporal clustering...")
            time.sleep(delay)

    log("Funding complete! All wallets have been queued for withdrawal from Binance.")

if __name__ == '__main__':
    main()
