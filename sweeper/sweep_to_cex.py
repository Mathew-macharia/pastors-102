import csv
import json
import os
import sys
import time
import random
import requests
from datetime import datetime
from dotenv import load_dotenv

from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.compute_budget import set_compute_unit_price, set_compute_unit_limit

# Load environment variables
load_dotenv()
HELIUS_API_KEY = os.getenv('HELIUS_API_KEY')

# Paths
WALLETS_CSV = '../wallets/wallets.csv'
PRIVATE_DIR = '../wallets/private'
DEPOSITS_CSV = 'deposit_addresses.csv'
STATE_DIR = 'state'
STATE_FILE = f'{STATE_DIR}/sweep_state.json'
LOG_DIR = 'logs'
LOG_FILE = f'{LOG_DIR}/sweep.log'

# Constants
FEE_RESERVE_LAMPORTS = 500_000  # Leave 0.0005 SOL for base fee + priority fee
PRIORITY_MICRO_LAMPORTS = 200_000

# Delays to prevent clustering
MIN_DELAY_SEC = 30
MAX_DELAY_SEC = 120

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

def load_wallets_and_destinations() -> list:
    if not os.path.exists(WALLETS_CSV):
        log(f"ERROR: Wallets file {WALLETS_CSV} not found.")
        sys.exit(1)
        
    if not os.path.exists(DEPOSITS_CSV):
        log(f"ERROR: Deposits file {DEPOSITS_CSV} not found.")
        sys.exit(1)

    wallets = {}
    with open(WALLETS_CSV, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            wallets[row['index']] = {
                'pubkey': row['pubkey'],
                'private_file': f"{PRIVATE_DIR}/{row['pubkey']}.json"
            }

    pairs = []
    with open(DEPOSITS_CSV, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            idx = row['index']
            if idx in wallets:
                pairs.append({
                    'index': idx,
                    'source_pubkey': wallets[idx]['pubkey'],
                    'source_private_file': wallets[idx]['private_file'],
                    'destination_pubkey': row['deposit_pubkey']
                })
            else:
                log(f"WARNING: Deposit address for index {idx} found, but no matching wallet.")
                
    return pairs

def main():
    if not HELIUS_API_KEY:
        log("ERROR: HELIUS_API_KEY must be set in .env")
        sys.exit(1)

    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    rpc_url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    sender_url = f"https://sender.helius-rpc.com/fast?api-key={HELIUS_API_KEY}"
    client = Client(rpc_url)

    pairs = load_wallets_and_destinations()
    state = load_state()
    
    total = len(pairs)
    log(f"Loaded {total} wallet->deposit pairs.")

    for i, pair in enumerate(pairs):
        idx = pair['index']
        src_pubkey_str = pair['source_pubkey']
        dst_pubkey_str = pair['destination_pubkey']
        
        # Check if already swept
        if src_pubkey_str in state and state[src_pubkey_str].get('status') == 'swept':
            log(f"[{i+1}/{total}] Wallet {idx} ({src_pubkey_str}) already swept. Skipping.")
            continue

        log(f"[{i+1}/{total}] Sweeping Wallet {idx}: {src_pubkey_str} -> {dst_pubkey_str}")

        # Load keypair
        try:
            with open(pair['source_private_file'], 'r') as f:
                secret = json.load(f)
                kp = Keypair.from_bytes(bytes(secret))
        except Exception as e:
            log(f"  -> ERROR reading keypair: {e}")
            continue

        src_pubkey = Pubkey.from_string(src_pubkey_str)
        dst_pubkey = Pubkey.from_string(dst_pubkey_str)

        # Get balance
        try:
            resp = client.get_balance(src_pubkey)
            balance = resp.value
        except Exception as e:
            log(f"  -> ERROR getting balance: {e}")
            continue

        log(f"  -> Balance: {balance / 10**9} SOL")

        if balance <= FEE_RESERVE_LAMPORTS:
            log(f"  -> Balance too low to sweep (needs at least {FEE_RESERVE_LAMPORTS / 10**9} SOL for fees). Skipping.")
            state[src_pubkey_str] = {
                'index': idx,
                'status': 'insufficient_funds',
                'balance': balance
            }
            save_state(state)
            continue

        transfer_amount = balance - FEE_RESERVE_LAMPORTS
        log(f"  -> Sending {transfer_amount / 10**9} SOL...")

        try:
            # Build instructions
            ixs = [
                set_compute_unit_limit(500),
                set_compute_unit_price(PRIORITY_MICRO_LAMPORTS),
                transfer(TransferParams(
                    from_pubkey=src_pubkey,
                    to_pubkey=dst_pubkey,
                    lamports=transfer_amount
                ))
            ]

            blockhash = client.get_latest_blockhash().value.blockhash
            msg = MessageV0.try_compile(src_pubkey, ixs, [], blockhash)
            tx = VersionedTransaction(msg, [kp])

            # Send via Helius Sender
            raw_tx = bytes(tx)
            import base64
            b64_tx = base64.b64encode(raw_tx).decode('utf-8')
            
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [b64_tx, {"encoding": "base64", "maxRetries": 0, "skipPreflight": True}]
            }
            
            res = requests.post(sender_url, json=payload)
            res_data = res.json()
            
            if 'error' in res_data:
                log(f"  -> ERROR sending tx: {res_data['error']}")
                continue
                
            sig = res_data['result']
            log(f"  -> Sent! Signature: {sig}")
            
            # Wait for confirmation
            log("  -> Waiting for confirmation...")
            confirmed = False
            for _ in range(15):
                time.sleep(2)
                status_res = requests.post(rpc_url, json={
                    "jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses", 
                    "params": [[sig], {"searchTransactionHistory": False}]
                }).json()
                
                if 'result' in status_res and status_res['result']['value'][0] is not None:
                    val = status_res['result']['value'][0]
                    if val.get('err'):
                        log(f"  -> Transaction failed on-chain: {val['err']}")
                        break
                    if val.get('confirmationStatus') in ['confirmed', 'finalized']:
                        confirmed = True
                        break

            if confirmed:
                log("  -> Confirmed!")
                state[src_pubkey_str] = {
                    'index': idx,
                    'status': 'swept',
                    'amount_swept': transfer_amount,
                    'signature': sig,
                    'destination': dst_pubkey_str,
                    'timestamp': datetime.utcnow().isoformat() + "Z"
                }
                save_state(state)
            else:
                log("  -> Timeout or failed. Will retry on next run.")
                
        except Exception as e:
            log(f"  -> UNEXPECTED ERROR: {e}")

        # Random delay between sweeps to avoid temporal clustering
        if i < total - 1:
            delay = random.randint(MIN_DELAY_SEC, MAX_DELAY_SEC)
            log(f"Sleeping {delay} seconds to defeat temporal clustering...")
            time.sleep(delay)

    log("Sweep complete!")

if __name__ == '__main__':
    main()
