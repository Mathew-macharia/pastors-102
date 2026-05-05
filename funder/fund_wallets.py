#!/usr/bin/env python3
"""
Production Solana batch funder.

Phase 2 of the pipeline: read the 40 wallet pubkeys produced by phase 1
(`../wallets/wallets.csv`) and send randomized small amounts of SOL to each
one, from a treasury keypair, via Helius Sender (ultra-low-latency dual /
SWQoS routing).

Per-transaction (every wallet):
- ComputeBudgetProgram.SetComputeUnitLimit  (tight: 1500 CU)
- ComputeBudgetProgram.SetComputeUnitPrice  (random microLamports/CU,
  jittered around Helius `getPriorityFeeEstimate` recommendation)
- SystemProgram.transfer                    (random amount in SOL range)
- SystemProgram.transfer                    (random Jito/Helius tip,
  random one of the 10 designated tip accounts)

All four instructions in one VersionedTransaction with a v0 message.
Submitted via Helius Sender with `skipPreflight=true` and `maxRetries=0`
(both required by Sender).

Camouflage:
- Random amount per wallet: 0.09 - 0.11 SOL (configurable)
- Random delay between wallets: 30s - 300s (configurable)
- Random priority fee jitter: ~0.85x - 1.25x of network estimate
- Random tip account from the 10-pool

State / resumability:
- `state/funding_state.json` records every wallet's status
  (pending|sent|confirmed|failed) with txid, slot, fee, tip, timestamp.
- A re-run skips already-confirmed wallets and retries failed ones.
- If a prior run was interrupted while a tx was in-flight (`sent` but not
  `confirmed`), the next run polls that txid first instead of double-spending.

References:
- Helius Sender:    https://www.helius.dev/docs/sending-transactions/sender
- Priority fees:    https://solana.com/docs/core/fees/compute-budget
- Priority est:     https://www.helius.dev/docs/api-reference/priority-fee/getpriorityfeeestimate
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import logging
import os
import random
import stat
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import requests
    from dotenv import load_dotenv
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer
    from solders.transaction import VersionedTransaction
except ImportError as e:
    sys.stderr.write(
        f"ERROR: missing dependency ({e.name}). "
        "Run: pip install -r requirements.txt\n"
    )
    sys.exit(2)


# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent
WALLETS_CSV = ROOT.parent / "wallets" / "wallets.csv"
TREASURY_DIR = ROOT / "treasury"
TREASURY_KEYPAIR = TREASURY_DIR / "treasury.json"
LOGS_DIR = ROOT / "logs"
STATE_DIR = ROOT / "state"
STATE_FILE = STATE_DIR / "funding_state.json"
FUNDING_LOG = LOGS_DIR / "funding.log"
ENV_FILE = ROOT / ".env"

# Helius / Jito designated tip accounts (mainnet-beta).
# Source: https://www.helius.dev/docs/sending-transactions/sender#designated-tip-accounts
TIP_ACCOUNTS: list[str] = [
    "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE",
    "D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ",
    "9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta",
    "5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn",
    "2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD",
    "2q5pghRs6arqVjRvT5gfgWfWcHWmw1ZuCzphgd5KfWGJ",
    "wyvPkWjVZz1M8fHQnMMCDTQDbkManefNNhweYk5WkcF",
    "3KCKozbAaF75qEU33jtzozcJ29yJuaLJTy2jFdzUY8bT",
    "4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey",
    "4TQLFNWK8AovT1gFvda5jfw2oJeRMKEmw7aH6MGBJ3or",
]

LAMPORTS_PER_SOL = 1_000_000_000

# Sender minimums per Helius docs.
SWQOS_MIN_TIP_LAMPORTS = 5_000        # 0.000005 SOL  (?swqos_only=true)
DUAL_MIN_TIP_LAMPORTS = 200_000       # 0.0002 SOL    (default Sender)

# Real CU usage of (limit + price + 2 transfers) ~= 600 CU. 1500 leaves
# safety margin and keeps the priority-fee budget tight (priority_fee in
# lamports = micro_price * unit_limit / 1_000_000).
COMPUTE_UNIT_LIMIT = 1500
BASE_FEE_LAMPORTS = 5_000              # 1 signature * 5000 lamports/sig

DEFAULT_MIN_AMOUNT_SOL = 0.09
DEFAULT_MAX_AMOUNT_SOL = 0.11
DEFAULT_MIN_DELAY = 30
DEFAULT_MAX_DELAY = 300
DEFAULT_PRIO_MIN = 50_000              # microLamports/CU
DEFAULT_PRIO_MAX = 200_000

SENDER_ENDPOINT_SWQOS = "https://sender.helius-rpc.com/fast?swqos_only=true"
SENDER_ENDPOINT_DUAL = "https://sender.helius-rpc.com/fast"

CONFIRMATION_TIMEOUT_S = 60
CONFIRMATION_POLL_S = 2
RPC_TIMEOUT_S = 15


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
@dataclass
class WalletFunding:
    index: int
    pubkey: str
    amount_lamports: int = 0
    priority_micro_lamports: int = 0
    tip_lamports: int = 0
    tip_account: str = ""
    txid: str = ""
    slot: int = 0
    status: str = "pending"  # pending | sent | confirmed | failed
    error: str = ""
    funded_at_utc: str = ""


@dataclass
class FundingState:
    started_at_utc: str = ""
    treasury_pubkey: str = ""
    rpc_url: str = ""
    sender_endpoint: str = ""
    wallets: list[WalletFunding] = field(default_factory=list)


def load_state() -> Optional[FundingState]:
    if not STATE_FILE.exists():
        return None
    raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    s = FundingState(
        started_at_utc=raw.get("started_at_utc", ""),
        treasury_pubkey=raw.get("treasury_pubkey", ""),
        rpc_url=raw.get("rpc_url", ""),
        sender_endpoint=raw.get("sender_endpoint", ""),
    )
    s.wallets = [WalletFunding(**w) for w in raw.get("wallets", [])]
    return s


def save_state(state: FundingState) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    payload = {
        "started_at_utc": state.started_at_utc,
        "treasury_pubkey": state.treasury_pubkey,
        "rpc_url": state.rpc_url,
        "sender_endpoint": state.sender_endpoint,
        "wallets": [asdict(w) for w in state.wallets],
    }
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# --------------------------------------------------------------------------- #
# Logging / FS helpers
# --------------------------------------------------------------------------- #
def setup_logging() -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("funder")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter(
        fmt="%(asctime)sZ | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    fmt.converter = time.gmtime
    fh = logging.FileHandler(FUNDING_LOG, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def secure_chmod(p: Path) -> None:
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def secure_chmod_dir(p: Path) -> None:
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    except OSError:
        pass


def load_treasury_keypair() -> Keypair:
    if not TREASURY_KEYPAIR.exists():
        sys.stderr.write(
            f"ERROR: treasury keypair not found at {TREASURY_KEYPAIR}\n"
            "Generate one:\n"
            f"  mkdir -p {TREASURY_DIR}\n"
            "  solana-keygen new --no-bip39-passphrase --outfile "
            f"{TREASURY_KEYPAIR}\n"
        )
        sys.exit(3)
    raw = json.loads(TREASURY_KEYPAIR.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or len(raw) != 64:
        raise RuntimeError(f"unexpected treasury keypair shape in {TREASURY_KEYPAIR}")
    secure_chmod_dir(TREASURY_DIR)
    secure_chmod(TREASURY_KEYPAIR)
    return Keypair.from_bytes(bytes(raw))


def read_recipients() -> list[str]:
    if not WALLETS_CSV.exists():
        sys.stderr.write(
            f"ERROR: wallets.csv not found at {WALLETS_CSV}\n"
            "Did you run phase 1 (../wallets/generate_wallets.py)?\n"
        )
        sys.exit(4)
    rows: list[str] = []
    with WALLETS_CSV.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pk = (row.get("pubkey") or "").strip()
            if pk:
                rows.append(pk)
    seen: set[str] = set()
    out: list[str] = []
    for pk in rows:
        if pk not in seen:
            seen.add(pk)
            out.append(pk)
    return out


# --------------------------------------------------------------------------- #
# RPC
# --------------------------------------------------------------------------- #
class RpcError(Exception):
    pass


def rpc_post(rpc_url: str, method: str, params: list, timeout: int = RPC_TIMEOUT_S) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": str(int(time.time() * 1000)),
        "method": method,
        "params": params,
    }
    r = requests.post(rpc_url, json=payload, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RpcError(f"{method}: {j['error']}")
    return j["result"]


def get_latest_blockhash(rpc_url: str) -> tuple[Hash, int]:
    res = rpc_post(rpc_url, "getLatestBlockhash", [{"commitment": "confirmed"}])
    bh = res["value"]["blockhash"]
    last_valid = int(res["value"]["lastValidBlockHeight"])
    return Hash.from_string(bh), last_valid


def get_balance_lamports(rpc_url: str, pubkey: str) -> int:
    res = rpc_post(rpc_url, "getBalance", [pubkey, {"commitment": "confirmed"}])
    return int(res["value"])


def get_priority_fee_estimate(rpc_url: str, account_keys: list[str]) -> Optional[int]:
    """Helius `getPriorityFeeEstimate`. Returns None if Helius is unreachable."""
    try:
        res = rpc_post(
            rpc_url,
            "getPriorityFeeEstimate",
            [{"accountKeys": account_keys, "options": {"recommended": True}}],
            timeout=10,
        )
        v = res.get("priorityFeeEstimate")
        return int(v) if v is not None else None
    except Exception:
        return None


def confirm_signature(rpc_url: str, sig: str, timeout_s: int = CONFIRMATION_TIMEOUT_S) -> Optional[int]:
    """Poll getSignatureStatuses until confirmed or timeout. Returns slot or None."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            res = rpc_post(
                rpc_url,
                "getSignatureStatuses",
                [[sig], {"searchTransactionHistory": True}],
            )
            v = res["value"][0]
            if v is None:
                time.sleep(CONFIRMATION_POLL_S)
                continue
            if v.get("err"):
                raise RpcError(f"transaction failed on-chain: {v['err']}")
            cs = v.get("confirmationStatus")
            if cs in ("confirmed", "finalized"):
                return int(v["slot"])
        except RpcError:
            raise
        except Exception:
            pass
        time.sleep(CONFIRMATION_POLL_S)
    return None


def send_via_sender(sender_url: str, raw_tx_b64: str) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": str(int(time.time() * 1000)),
        "method": "sendTransaction",
        "params": [
            raw_tx_b64,
            {"encoding": "base64", "skipPreflight": True, "maxRetries": 0},
        ],
    }
    r = requests.post(sender_url, json=payload, timeout=RPC_TIMEOUT_S)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RpcError(f"sender: {j['error']}")
    return str(j["result"])


# --------------------------------------------------------------------------- #
# Transaction building
# --------------------------------------------------------------------------- #
def build_transfer_tx(
    treasury: Keypair,
    recipient: Pubkey,
    amount_lamports: int,
    priority_micro_lamports: int,
    tip_lamports: int,
    tip_account: Pubkey,
    blockhash: Hash,
) -> bytes:
    ixs = [
        set_compute_unit_limit(COMPUTE_UNIT_LIMIT),
        set_compute_unit_price(priority_micro_lamports),
        transfer(TransferParams(
            from_pubkey=treasury.pubkey(),
            to_pubkey=recipient,
            lamports=amount_lamports,
        )),
        transfer(TransferParams(
            from_pubkey=treasury.pubkey(),
            to_pubkey=tip_account,
            lamports=tip_lamports,
        )),
    ]
    msg = MessageV0.try_compile(
        payer=treasury.pubkey(),
        instructions=ixs,
        address_lookup_table_accounts=[],
        recent_blockhash=blockhash,
    )
    tx = VersionedTransaction(msg, [treasury])
    return bytes(tx)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Production Solana batch funder using Helius Sender.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--min-amount-sol", type=float, default=DEFAULT_MIN_AMOUNT_SOL)
    ap.add_argument("--max-amount-sol", type=float, default=DEFAULT_MAX_AMOUNT_SOL)
    ap.add_argument("--min-delay", type=int, default=DEFAULT_MIN_DELAY,
                    help="Min delay between funding txs (seconds)")
    ap.add_argument("--max-delay", type=int, default=DEFAULT_MAX_DELAY,
                    help="Max delay between funding txs (seconds)")
    ap.add_argument("--prio-min", type=int, default=DEFAULT_PRIO_MIN,
                    help="Min priority fee (microLamports/CU)")
    ap.add_argument("--prio-max", type=int, default=DEFAULT_PRIO_MAX,
                    help="Max priority fee (microLamports/CU)")
    ap.add_argument("--dual", action="store_true",
                    help="Use dual-route Sender (Jito + validators) instead of "
                         "SWQoS-only. Enforces 0.0002 SOL min tip vs 0.000005 SOL.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Pre-flight everything but do not send transactions.")
    ap.add_argument("-n", "--limit", type=int, default=0,
                    help="Only fund the first N recipients (0 = all). For testing.")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE)

    api_key = os.environ.get("HELIUS_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write(
            "ERROR: HELIUS_API_KEY not set. Add it to .env or export it.\n"
            "Get a free key at https://dashboard.helius.dev/\n"
        )
        return 5
    rpc_url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"
    sender_url = SENDER_ENDPOINT_DUAL if args.dual else SENDER_ENDPOINT_SWQOS
    min_tip = DUAL_MIN_TIP_LAMPORTS if args.dual else SWQOS_MIN_TIP_LAMPORTS

    if args.min_amount_sol <= 0 or args.max_amount_sol < args.min_amount_sol:
        sys.stderr.write("ERROR: invalid --min-amount-sol / --max-amount-sol\n")
        return 2
    if args.min_delay < 0 or args.max_delay < args.min_delay:
        sys.stderr.write("ERROR: invalid --min-delay / --max-delay\n")
        return 2
    if args.prio_min < 0 or args.prio_max < args.prio_min:
        sys.stderr.write("ERROR: invalid --prio-min / --prio-max\n")
        return 2

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    logger = setup_logging()
    treasury = load_treasury_keypair()
    treasury_pk = str(treasury.pubkey())
    logger.info(f"treasury     : {treasury_pk}")
    logger.info(f"sender       : {sender_url}")
    logger.info(f"min tip      : {min_tip} lamports ({min_tip / LAMPORTS_PER_SOL:.6f} SOL)")
    logger.info(f"amount range : {args.min_amount_sol} - {args.max_amount_sol} SOL")
    logger.info(f"delay range  : {args.min_delay} - {args.max_delay} s")
    logger.info(f"prio range   : {args.prio_min} - {args.prio_max} microLamports/CU")

    recipients = read_recipients()
    if args.limit > 0:
        recipients = recipients[: args.limit]
    logger.info(f"loaded {len(recipients)} recipients from {WALLETS_CSV}")

    state = load_state()
    if state is None:
        state = FundingState(
            started_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            treasury_pubkey=treasury_pk,
            rpc_url=rpc_url,
            sender_endpoint=sender_url,
            wallets=[WalletFunding(index=i + 1, pubkey=pk) for i, pk in enumerate(recipients)],
        )
        save_state(state)
        logger.info("created new funding state")
    else:
        if state.treasury_pubkey != treasury_pk:
            sys.stderr.write(
                f"ERROR: state file treasury mismatch (state={state.treasury_pubkey} vs "
                f"current={treasury_pk}). Delete {STATE_FILE} to start fresh.\n"
            )
            return 6
        existing = {w.pubkey for w in state.wallets}
        for pk in recipients:
            if pk not in existing:
                state.wallets.append(WalletFunding(index=len(state.wallets) + 1, pubkey=pk))
                existing.add(pk)
        save_state(state)
        logger.info("resumed existing funding state")

    pending = [w for w in state.wallets if w.status != "confirmed"]
    logger.info(f"pending/retryable: {len(pending)}/{len(state.wallets)}")

    treasury_balance = get_balance_lamports(rpc_url, treasury_pk)
    max_per_tx = (
        int(args.max_amount_sol * LAMPORTS_PER_SOL)
        + int(min_tip * 1.5)
        + BASE_FEE_LAMPORTS
        + (args.prio_max * COMPUTE_UNIT_LIMIT) // 1_000_000
    )
    needed = max_per_tx * len(pending) + 1_000_000  # 0.001 SOL slack
    logger.info(
        f"treasury balance: {treasury_balance / LAMPORTS_PER_SOL:.6f} SOL "
        f"({treasury_balance} lamports). Worst-case need for pending: "
        f"{needed / LAMPORTS_PER_SOL:.6f} SOL"
    )
    if treasury_balance < needed:
        sys.stderr.write(
            f"ERROR: treasury underfunded. Have {treasury_balance / LAMPORTS_PER_SOL:.6f} SOL, "
            f"need at least {needed / LAMPORTS_PER_SOL:.6f} SOL.\n"
            "Top up the treasury and re-run.\n"
        )
        return 7

    if args.dry_run:
        logger.info("DRY RUN: pre-flight passed. No transactions sent.")
        return 0

    base_estimate = get_priority_fee_estimate(rpc_url, [treasury_pk])
    if base_estimate is not None:
        logger.info(f"helius priority fee estimate (recommended): {base_estimate} microLamports/CU")

    rng = random.SystemRandom()
    confirmed = sum(1 for w in state.wallets if w.status == "confirmed")

    for wf in state.wallets:
        if wf.status == "confirmed":
            continue

        # Resume safety: a prior run sent a tx but didn't confirm. Poll
        # before building a new one to avoid double-spending.
        if wf.status == "sent" and wf.txid:
            logger.info(f"[{wf.index:02d}] resume: polling prior txid {wf.txid}")
            try:
                slot = confirm_signature(rpc_url, wf.txid, timeout_s=CONFIRMATION_TIMEOUT_S)
            except RpcError as e:
                slot = None
                wf.error = f"resume_check: {e}"
            if slot is not None:
                wf.slot = slot
                wf.status = "confirmed"
                save_state(state)
                confirmed += 1
                logger.info(f"[{wf.index:02d}] confirmed prior tx slot={slot}")
                continue
            wf.status = "failed"
            if not wf.error:
                wf.error = "confirmation_timeout_on_resume"
            save_state(state)

        try:
            recipient_pk = Pubkey.from_string(wf.pubkey)

            amount_sol = rng.uniform(args.min_amount_sol, args.max_amount_sol)
            amount_lamports = int(amount_sol * LAMPORTS_PER_SOL)

            est = get_priority_fee_estimate(rpc_url, [treasury_pk]) or 0
            lo = max(args.prio_min, int(est * 0.85))
            hi = max(lo + 1, max(args.prio_max, int(est * 1.25)))
            priority_micro_lamports = rng.randint(lo, hi)

            tip_lamports = rng.randint(min_tip, int(min_tip * 1.5))
            tip_account_str = rng.choice(TIP_ACCOUNTS)
            tip_account = Pubkey.from_string(tip_account_str)

            blockhash, _last_valid = get_latest_blockhash(rpc_url)

            raw = build_transfer_tx(
                treasury=treasury,
                recipient=recipient_pk,
                amount_lamports=amount_lamports,
                priority_micro_lamports=priority_micro_lamports,
                tip_lamports=tip_lamports,
                tip_account=tip_account,
                blockhash=blockhash,
            )
            raw_b64 = base64.b64encode(raw).decode()

            txid = send_via_sender(sender_url, raw_b64)
            wf.txid = txid
            wf.status = "sent"
            wf.amount_lamports = amount_lamports
            wf.priority_micro_lamports = priority_micro_lamports
            wf.tip_lamports = tip_lamports
            wf.tip_account = tip_account_str
            wf.funded_at_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
            wf.error = ""
            save_state(state)

            logger.info(
                f"[{wf.index:02d}/{len(state.wallets)}] sent  txid={txid} "
                f"amount={amount_sol:.6f}SOL prio={priority_micro_lamports}mLam/CU "
                f"tip={tip_lamports}L tip_acct={tip_account_str[:8]}... -> {wf.pubkey}"
            )

            slot = confirm_signature(rpc_url, txid, timeout_s=CONFIRMATION_TIMEOUT_S)
            if slot is None:
                wf.status = "failed"
                wf.error = "confirmation_timeout"
                logger.warning(
                    f"[{wf.index:02d}] confirmation timeout for {txid} - "
                    f"check Solscan before re-running (tx may still land)"
                )
            else:
                wf.slot = slot
                wf.status = "confirmed"
                confirmed += 1
                logger.info(f"[{wf.index:02d}] confirmed slot={slot}")
            save_state(state)

        except Exception as e:
            wf.status = "failed"
            wf.error = str(e)
            save_state(state)
            logger.error(f"[{wf.index:02d}] FAILED: {e}")

        if wf.index < len(state.wallets):
            d = rng.uniform(args.min_delay, args.max_delay)
            logger.info(f"sleep {d:.1f}s before next wallet")
            time.sleep(d)

    logger.info(
        f"done. confirmed {confirmed}/{len(state.wallets)} wallets. "
        f"state={STATE_FILE}  log={FUNDING_LOG}"
    )
    return 0 if confirmed == len(state.wallets) else 1


if __name__ == "__main__":
    sys.exit(main())
