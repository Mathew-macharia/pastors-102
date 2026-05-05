#!/usr/bin/env python3
"""
Production Solana multi-hop funder with Jupiter swap obfuscation.

Pipeline (every stage is fully resumable):

    Stage 1:  Treasury -> 8 fresh A-wallets         (SOL transfers via Sender)
    Stage 2:  Each A-wallet swaps SOL -> USDC       (Jupiter v1)
    Stage 3:  Each A-wallet -> a fresh B-wallet     (USDC transfers via Sender)
    Stage 4:  Each B-wallet swaps USDC -> SOL       (Jupiter v1)
    Stage 5:  Each B-wallet -> 5 final wallets      (SOL transfers via Sender)

Total: 72 transactions, 16 fresh intermediate wallets.

Reads:
    ../funder/treasury/treasury.json   (treasury keypair)
    ../wallets/wallets.csv             (40 final pubkeys)

Writes:
    intermediates/A_<idx>.json + .seed.txt   (chmod 600 each)
    intermediates/B_<idx>.json + .seed.txt   (chmod 600 each)
    state/multihop_state.json                (resumable state)
    logs/multihop.log                        (UTC log)

OPSEC reminder (read README.md):
    Multi-hop with swaps RAISES the bar for naive automated clustering.
    It does NOT defeat serious chain-analytics services (Chainalysis,
    TRM Labs, Arkham, Binance's own AML platform). In March 2026 a
    24,500 SOL transfer to Binance using exactly this relay-and-swap
    pattern was publicly flagged. This script is intentionally
    transparent about that.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import logging
import os
import random
import shutil
import stat
import subprocess
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
    from spl.token.constants import TOKEN_PROGRAM_ID
    from spl.token.instructions import (
        TransferCheckedParams,
        create_associated_token_account_idempotent,
        get_associated_token_address,
        transfer_checked,
    )
except ImportError as e:
    sys.stderr.write(
        f"ERROR: missing dependency ({e.name}). "
        "Run: pip install -r requirements.txt\n"
    )
    sys.exit(2)

import jupiter as jup


# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent
WALLETS_CSV = ROOT.parent / "wallets" / "wallets.csv"
TREASURY_KEYPAIR = ROOT.parent / "funder" / "treasury" / "treasury.json"

INTER_DIR = ROOT / "intermediates"
STATE_DIR = ROOT / "state"
LOGS_DIR = ROOT / "logs"
STATE_FILE = STATE_DIR / "multihop_state.json"
MULTIHOP_LOG = LOGS_DIR / "multihop.log"
ENV_FILE = ROOT / ".env"

# Helius / Jito tip accounts (mainnet-beta). Source: Helius Sender docs.
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
SWQOS_MIN_TIP_LAMPORTS = 5_000      # 0.000005 SOL
DUAL_MIN_TIP_LAMPORTS = 200_000     # 0.0002 SOL
COMPUTE_UNIT_LIMIT_TRANSFER = 1500
COMPUTE_UNIT_LIMIT_USDC_TRANSFER = 60_000   # ATA create+idempotent + transfer_checked
BASE_FEE_LAMPORTS = 5_000

DEFAULT_NUM_CHAINS = 8
DEFAULT_NUM_FINALS = 40
DEFAULT_TREASURY_PER_CHAIN_SOL = 0.65
DEFAULT_FINAL_MIN_SOL = 0.09
DEFAULT_FINAL_MAX_SOL = 0.11
DEFAULT_MIN_DELAY = 30
DEFAULT_MAX_DELAY = 300
DEFAULT_PRIO_MIN = 50_000
DEFAULT_PRIO_MAX = 200_000
DEFAULT_SLIPPAGE_BPS = 100          # 1.00 %

SENDER_ENDPOINT_SWQOS = "https://sender.helius-rpc.com/fast?swqos_only=true"
SENDER_ENDPOINT_DUAL = "https://sender.helius-rpc.com/fast"

CONFIRMATION_TIMEOUT_S = 90
CONFIRMATION_POLL_S = 2
RPC_TIMEOUT_S = 20

USDC_MINT = jup.USDC_MINT
SOL_MINT = jup.SOL_MINT


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
@dataclass
class StageOp:
    txid: str = ""
    slot: int = 0
    status: str = "pending"     # pending | sent | confirmed | failed
    error: str = ""
    completed_at_utc: str = ""


@dataclass
class FinalDistribution:
    final_pubkey: str
    amount_lamports: int = 0
    op: StageOp = field(default_factory=StageOp)


@dataclass
class Chain:
    chain_id: int
    a_pubkey: str = ""
    b_pubkey: str = ""
    a_keypair_path: str = ""
    b_keypair_path: str = ""
    final_pubkeys: list[str] = field(default_factory=list)

    # Stage 1: treasury -> A (SOL)
    stage1_amount_lamports: int = 0
    stage1: StageOp = field(default_factory=StageOp)

    # Stage 2: A swap SOL -> USDC
    stage2_input_lamports: int = 0
    stage2_output_atoms: int = 0
    stage2: StageOp = field(default_factory=StageOp)

    # Stage 3: A -> B (USDC)
    stage3_amount_atoms: int = 0
    stage3: StageOp = field(default_factory=StageOp)

    # Stage 4: B swap USDC -> SOL
    stage4_input_atoms: int = 0
    stage4_output_lamports: int = 0
    stage4: StageOp = field(default_factory=StageOp)

    # Stage 5: B -> 5 finals (SOL)
    stage5_distributions: list[FinalDistribution] = field(default_factory=list)


@dataclass
class MultiHopState:
    started_at_utc: str = ""
    treasury_pubkey: str = ""
    rpc_url: str = ""
    sender_endpoint: str = ""
    num_chains: int = 0
    chains: list[Chain] = field(default_factory=list)


def _stage_op_from(d: dict) -> StageOp:
    return StageOp(
        txid=d.get("txid", ""),
        slot=int(d.get("slot", 0)),
        status=d.get("status", "pending"),
        error=d.get("error", ""),
        completed_at_utc=d.get("completed_at_utc", ""),
    )


def _final_dist_from(d: dict) -> FinalDistribution:
    return FinalDistribution(
        final_pubkey=d.get("final_pubkey", ""),
        amount_lamports=int(d.get("amount_lamports", 0)),
        op=_stage_op_from(d.get("op", {})),
    )


def _chain_from(d: dict) -> Chain:
    return Chain(
        chain_id=int(d.get("chain_id", 0)),
        a_pubkey=d.get("a_pubkey", ""),
        b_pubkey=d.get("b_pubkey", ""),
        a_keypair_path=d.get("a_keypair_path", ""),
        b_keypair_path=d.get("b_keypair_path", ""),
        final_pubkeys=list(d.get("final_pubkeys", [])),
        stage1_amount_lamports=int(d.get("stage1_amount_lamports", 0)),
        stage1=_stage_op_from(d.get("stage1", {})),
        stage2_input_lamports=int(d.get("stage2_input_lamports", 0)),
        stage2_output_atoms=int(d.get("stage2_output_atoms", 0)),
        stage2=_stage_op_from(d.get("stage2", {})),
        stage3_amount_atoms=int(d.get("stage3_amount_atoms", 0)),
        stage3=_stage_op_from(d.get("stage3", {})),
        stage4_input_atoms=int(d.get("stage4_input_atoms", 0)),
        stage4_output_lamports=int(d.get("stage4_output_lamports", 0)),
        stage4=_stage_op_from(d.get("stage4", {})),
        stage5_distributions=[_final_dist_from(x) for x in d.get("stage5_distributions", [])],
    )


def load_state() -> Optional[MultiHopState]:
    if not STATE_FILE.exists():
        return None
    raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    s = MultiHopState(
        started_at_utc=raw.get("started_at_utc", ""),
        treasury_pubkey=raw.get("treasury_pubkey", ""),
        rpc_url=raw.get("rpc_url", ""),
        sender_endpoint=raw.get("sender_endpoint", ""),
        num_chains=int(raw.get("num_chains", 0)),
        chains=[_chain_from(c) for c in raw.get("chains", [])],
    )
    return s


def save_state(state: MultiHopState) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    payload = {
        "started_at_utc": state.started_at_utc,
        "treasury_pubkey": state.treasury_pubkey,
        "rpc_url": state.rpc_url,
        "sender_endpoint": state.sender_endpoint,
        "num_chains": state.num_chains,
        "chains": [asdict(c) for c in state.chains],
    }
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# --------------------------------------------------------------------------- #
# Logging / FS helpers
# --------------------------------------------------------------------------- #
def setup_logging() -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("multihop")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter(
        fmt="%(asctime)sZ | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    fmt.converter = time.gmtime
    fh = logging.FileHandler(MULTIHOP_LOG, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def secure_chmod_file(p: Path) -> None:
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def secure_chmod_dir(p: Path) -> None:
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    except OSError:
        pass


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Pre-flight: solana-keygen, treasury, recipients
# --------------------------------------------------------------------------- #
def require_solana_keygen() -> str:
    p = shutil.which("solana-keygen")
    if not p:
        sys.stderr.write(
            "ERROR: `solana-keygen` not on PATH. "
            "Run ../wallets/setup.sh or install Solana CLI first.\n"
        )
        sys.exit(3)
    return p


def load_treasury_keypair() -> Keypair:
    if not TREASURY_KEYPAIR.exists():
        sys.stderr.write(
            f"ERROR: treasury keypair not found at {TREASURY_KEYPAIR}\n"
            "Run ../funder/setup.sh first to create the treasury wallet.\n"
        )
        sys.exit(4)
    raw = json.loads(TREASURY_KEYPAIR.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or len(raw) != 64:
        raise RuntimeError(f"unexpected treasury keypair shape in {TREASURY_KEYPAIR}")
    return Keypair.from_bytes(bytes(raw))


def read_finals() -> list[str]:
    if not WALLETS_CSV.exists():
        sys.stderr.write(
            f"ERROR: wallets.csv not found at {WALLETS_CSV}\n"
            "Did you run phase 1 (../wallets/generate_wallets.py)?\n"
        )
        sys.exit(5)
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
# Fresh wallet generation (subprocess solana-keygen)
# --------------------------------------------------------------------------- #
def generate_fresh_wallet(label: str, idx: int, keygen_bin: str) -> tuple[str, Path, Path]:
    """
    Run `solana-keygen new` to create a fresh keypair. Capture pubkey + seed
    phrase from stdout. Move file to its final per-pubkey location and chmod 600.
    Returns (pubkey, json_path, seed_path).
    """
    INTER_DIR.mkdir(parents=True, exist_ok=True)
    secure_chmod_dir(INTER_DIR)

    tmp_json = INTER_DIR / f".tmp_{label}_{idx:02d}.json"
    cmd = [
        keygen_bin, "new",
        "--no-bip39-passphrase",
        "--force",
        "--outfile", str(tmp_json),
    ]
    proc = subprocess.run(cmd, input="", capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"solana-keygen failed (rc={proc.returncode}): {proc.stderr.strip()!r}")

    pubkey: Optional[str] = None
    seed_phrase: Optional[str] = None
    lines = [ln.rstrip() for ln in proc.stdout.splitlines()]
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("pubkey:") and pubkey is None:
            pubkey = s.split(":", 1)[1].strip()
        if s.startswith("Save this seed phrase") and seed_phrase is None:
            for j in range(i + 1, len(lines)):
                cand = lines[j].strip()
                if cand and not cand.startswith("="):
                    seed_phrase = cand
                    break
    if not pubkey or not seed_phrase:
        raise RuntimeError(f"failed to parse keygen output:\n{proc.stdout}\n{proc.stderr}")

    final_json = INTER_DIR / f"{label}_{idx:02d}_{pubkey}.json"
    tmp_json.replace(final_json)
    secure_chmod_file(final_json)
    seed_path = INTER_DIR / f"{label}_{idx:02d}_{pubkey}.seed.txt"
    seed_path.write_text(seed_phrase + "\n", encoding="utf-8")
    secure_chmod_file(seed_path)
    return pubkey, final_json, seed_path


def load_keypair_from(path: Path) -> Keypair:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or len(raw) != 64:
        raise RuntimeError(f"unexpected keypair shape in {path}")
    return Keypair.from_bytes(bytes(raw))


# --------------------------------------------------------------------------- #
# RPC helpers
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


def get_latest_blockhash(rpc_url: str) -> Hash:
    res = rpc_post(rpc_url, "getLatestBlockhash", [{"commitment": "confirmed"}])
    return Hash.from_string(res["value"]["blockhash"])


def get_balance_lamports(rpc_url: str, pubkey: str) -> int:
    res = rpc_post(rpc_url, "getBalance", [pubkey, {"commitment": "confirmed"}])
    return int(res["value"])


def get_token_balance_atoms(rpc_url: str, ata_pubkey: str) -> int:
    """getTokenAccountBalance returns 0 if account doesn't exist."""
    try:
        res = rpc_post(
            rpc_url,
            "getTokenAccountBalance",
            [ata_pubkey, {"commitment": "confirmed"}],
        )
        return int(res["value"]["amount"])
    except RpcError:
        return 0


def get_priority_fee_estimate(rpc_url: str, account_keys: list[str]) -> Optional[int]:
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


def send_via_rpc(rpc_url: str, raw_tx_b64: str) -> str:
    """Submit a tx via standard sendTransaction (used for Jupiter swaps)."""
    res = rpc_post(
        rpc_url,
        "sendTransaction",
        [raw_tx_b64, {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}],
    )
    return str(res)


# --------------------------------------------------------------------------- #
# Transaction builders
# --------------------------------------------------------------------------- #
def build_sol_transfer_tx(
    payer: Keypair,
    recipient: Pubkey,
    amount_lamports: int,
    priority_micro_lamports: int,
    tip_lamports: int,
    tip_account: Pubkey,
    blockhash: Hash,
) -> bytes:
    ixs = [
        set_compute_unit_limit(COMPUTE_UNIT_LIMIT_TRANSFER),
        set_compute_unit_price(priority_micro_lamports),
        transfer(TransferParams(
            from_pubkey=payer.pubkey(),
            to_pubkey=recipient,
            lamports=amount_lamports,
        )),
        transfer(TransferParams(
            from_pubkey=payer.pubkey(),
            to_pubkey=tip_account,
            lamports=tip_lamports,
        )),
    ]
    msg = MessageV0.try_compile(
        payer=payer.pubkey(),
        instructions=ixs,
        address_lookup_table_accounts=[],
        recent_blockhash=blockhash,
    )
    return bytes(VersionedTransaction(msg, [payer]))


def build_usdc_transfer_tx(
    sender: Keypair,
    recipient_pubkey: Pubkey,
    amount_atoms: int,
    priority_micro_lamports: int,
    tip_lamports: int,
    tip_account: Pubkey,
    blockhash: Hash,
) -> bytes:
    """
    SPL USDC transfer with idempotent ATA creation for the recipient.
    Sender pays the ATA rent (~0.00204 SOL) if recipient's ATA doesn't exist.
    """
    usdc_mint = Pubkey.from_string(USDC_MINT)
    sender_ata = get_associated_token_address(sender.pubkey(), usdc_mint)
    recipient_ata = get_associated_token_address(recipient_pubkey, usdc_mint)

    ix_create_ata = create_associated_token_account_idempotent(
        payer=sender.pubkey(),
        owner=recipient_pubkey,
        mint=usdc_mint,
    )
    ix_transfer = transfer_checked(TransferCheckedParams(
        program_id=TOKEN_PROGRAM_ID,
        source=sender_ata,
        mint=usdc_mint,
        dest=recipient_ata,
        owner=sender.pubkey(),
        amount=amount_atoms,
        decimals=jup.USDC_DECIMALS,
        signers=[],
    ))
    ix_tip = transfer(TransferParams(
        from_pubkey=sender.pubkey(),
        to_pubkey=tip_account,
        lamports=tip_lamports,
    ))

    ixs = [
        set_compute_unit_limit(COMPUTE_UNIT_LIMIT_USDC_TRANSFER),
        set_compute_unit_price(priority_micro_lamports),
        ix_create_ata,
        ix_transfer,
        ix_tip,
    ]
    msg = MessageV0.try_compile(
        payer=sender.pubkey(),
        instructions=ixs,
        address_lookup_table_accounts=[],
        recent_blockhash=blockhash,
    )
    return bytes(VersionedTransaction(msg, [sender]))


# --------------------------------------------------------------------------- #
# Common per-tx flow helpers
# --------------------------------------------------------------------------- #
@dataclass
class FeeJitter:
    priority_micro_lamports: int
    tip_lamports: int
    tip_account: str


def jitter_fees(
    rng: random.SystemRandom,
    rpc_url: str,
    fee_payer_pubkey: str,
    args: argparse.Namespace,
    min_tip: int,
) -> FeeJitter:
    est = get_priority_fee_estimate(rpc_url, [fee_payer_pubkey]) or 0
    lo = max(args.prio_min, int(est * 0.85))
    hi = max(lo + 1, max(args.prio_max, int(est * 1.25)))
    return FeeJitter(
        priority_micro_lamports=rng.randint(lo, hi),
        tip_lamports=rng.randint(min_tip, int(min_tip * 1.5)),
        tip_account=rng.choice(TIP_ACCOUNTS),
    )


def sleep_jitter(rng: random.SystemRandom, args: argparse.Namespace, logger: logging.Logger, label: str) -> None:
    d = rng.uniform(args.min_delay, args.max_delay)
    logger.info(f"[{label}] sleep {d:.1f}s")
    time.sleep(d)


# --------------------------------------------------------------------------- #
# Stage 1: Treasury -> A wallets (SOL transfer via Sender)
# --------------------------------------------------------------------------- #
def run_stage1(
    state: MultiHopState,
    treasury: Keypair,
    rpc_url: str,
    sender_url: str,
    min_tip: int,
    args: argparse.Namespace,
    logger: logging.Logger,
) -> None:
    rng = random.SystemRandom()
    treasury_pk = str(treasury.pubkey())
    per_chain = int(args.treasury_per_chain_sol * LAMPORTS_PER_SOL)

    for ch in state.chains:
        if ch.stage1.status == "confirmed":
            continue
        try:
            recipient = Pubkey.from_string(ch.a_pubkey)
            amount = per_chain  # exact amount per chain (deterministic; jitter happens later)
            fees = jitter_fees(rng, rpc_url, treasury_pk, args, min_tip)
            blockhash = get_latest_blockhash(rpc_url)

            raw = build_sol_transfer_tx(
                payer=treasury,
                recipient=recipient,
                amount_lamports=amount,
                priority_micro_lamports=fees.priority_micro_lamports,
                tip_lamports=fees.tip_lamports,
                tip_account=Pubkey.from_string(fees.tip_account),
                blockhash=blockhash,
            )
            txid = send_via_sender(sender_url, base64.b64encode(raw).decode())
            ch.stage1_amount_lamports = amount
            ch.stage1.txid = txid
            ch.stage1.status = "sent"
            save_state(state)
            logger.info(
                f"[stage1 chain {ch.chain_id:02d}] sent txid={txid} "
                f"amount={amount/LAMPORTS_PER_SOL:.6f} SOL -> A={ch.a_pubkey}"
            )

            slot = confirm_signature(rpc_url, txid)
            if slot is None:
                ch.stage1.status = "failed"
                ch.stage1.error = "confirmation_timeout"
                logger.warning(f"[stage1 chain {ch.chain_id:02d}] confirmation timeout: {txid}")
            else:
                ch.stage1.slot = slot
                ch.stage1.status = "confirmed"
                ch.stage1.completed_at_utc = now_utc()
                logger.info(f"[stage1 chain {ch.chain_id:02d}] confirmed slot={slot}")
            save_state(state)
        except Exception as e:
            ch.stage1.status = "failed"
            ch.stage1.error = str(e)
            save_state(state)
            logger.error(f"[stage1 chain {ch.chain_id:02d}] FAILED: {e}")

        if ch.chain_id < state.num_chains:
            sleep_jitter(rng, args, logger, f"stage1 chain {ch.chain_id:02d}")


# --------------------------------------------------------------------------- #
# Stage 2: A wallet swaps SOL -> USDC via Jupiter
# --------------------------------------------------------------------------- #
def run_stage2(
    state: MultiHopState,
    rpc_url: str,
    args: argparse.Namespace,
    logger: logging.Logger,
) -> None:
    rng = random.SystemRandom()

    for ch in state.chains:
        if ch.stage2.status == "confirmed":
            continue
        if ch.stage1.status != "confirmed":
            logger.warning(f"[stage2 chain {ch.chain_id:02d}] skipped (stage1 not confirmed)")
            continue
        try:
            a_kp = load_keypair_from(Path(ch.a_keypair_path))
            a_balance = get_balance_lamports(rpc_url, ch.a_pubkey)
            # leave buffer for stage3 SOL fees + ATA rent + tip:
            #   ~0.0021 ATA rent + 0.00001 fees + 0.000005 tip + 0.00001 prio = ~0.0022 SOL
            # plus a comfort buffer of 0.001 SOL -> 0.003 SOL = 3_000_000 lamports
            buffer_lamports = 3_000_000
            swap_in = a_balance - buffer_lamports
            if swap_in <= 0:
                raise RuntimeError(f"A has insufficient balance: {a_balance} lamports")
            ch.stage2_input_lamports = swap_in

            quote = jup.get_quote(
                input_mint=SOL_MINT,
                output_mint=USDC_MINT,
                amount=swap_in,
                slippage_bps=args.slippage_bps,
            )
            est_out = jup.expected_out(quote)

            est_prio = get_priority_fee_estimate(rpc_url, [ch.a_pubkey]) or 0
            cu_price = max(args.prio_min, est_prio)
            unsigned = jup.build_swap_tx(
                quote=quote,
                user_pubkey=ch.a_pubkey,
                compute_unit_price_micro_lamports=cu_price,
            )
            signed = jup.sign_jupiter_tx(unsigned, a_kp)
            txid = send_via_rpc(rpc_url, base64.b64encode(signed).decode())
            ch.stage2.txid = txid
            ch.stage2.status = "sent"
            save_state(state)
            logger.info(
                f"[stage2 chain {ch.chain_id:02d}] sent txid={txid} "
                f"swap in={swap_in/LAMPORTS_PER_SOL:.6f} SOL "
                f"quoted_out={est_out/(10**jup.USDC_DECIMALS):.4f} USDC"
            )

            slot = confirm_signature(rpc_url, txid)
            if slot is None:
                ch.stage2.status = "failed"
                ch.stage2.error = "confirmation_timeout"
                logger.warning(f"[stage2 chain {ch.chain_id:02d}] timeout {txid}")
                save_state(state)
                continue

            usdc_mint_pk = Pubkey.from_string(USDC_MINT)
            a_ata = str(get_associated_token_address(Pubkey.from_string(ch.a_pubkey), usdc_mint_pk))
            actual_atoms = get_token_balance_atoms(rpc_url, a_ata)
            ch.stage2_output_atoms = actual_atoms
            ch.stage2.slot = slot
            ch.stage2.status = "confirmed"
            ch.stage2.completed_at_utc = now_utc()
            logger.info(
                f"[stage2 chain {ch.chain_id:02d}] confirmed slot={slot} "
                f"actual_usdc={actual_atoms/(10**jup.USDC_DECIMALS):.4f}"
            )
            save_state(state)
        except Exception as e:
            ch.stage2.status = "failed"
            ch.stage2.error = str(e)
            save_state(state)
            logger.error(f"[stage2 chain {ch.chain_id:02d}] FAILED: {e}")

        if ch.chain_id < state.num_chains:
            sleep_jitter(rng, args, logger, f"stage2 chain {ch.chain_id:02d}")


# --------------------------------------------------------------------------- #
# Stage 3: A -> B (USDC transfer with idempotent ATA creation)
# --------------------------------------------------------------------------- #
def run_stage3(
    state: MultiHopState,
    rpc_url: str,
    sender_url: str,
    min_tip: int,
    args: argparse.Namespace,
    logger: logging.Logger,
) -> None:
    rng = random.SystemRandom()

    for ch in state.chains:
        if ch.stage3.status == "confirmed":
            continue
        if ch.stage2.status != "confirmed":
            logger.warning(f"[stage3 chain {ch.chain_id:02d}] skipped (stage2 not confirmed)")
            continue
        try:
            a_kp = load_keypair_from(Path(ch.a_keypair_path))
            usdc_mint_pk = Pubkey.from_string(USDC_MINT)
            a_ata = str(get_associated_token_address(a_kp.pubkey(), usdc_mint_pk))
            a_atoms = get_token_balance_atoms(rpc_url, a_ata)
            if a_atoms <= 0:
                raise RuntimeError(f"A has 0 USDC atoms (ata={a_ata})")
            # Send 100% of A's USDC to B
            ch.stage3_amount_atoms = a_atoms

            fees = jitter_fees(rng, rpc_url, ch.a_pubkey, args, min_tip)
            blockhash = get_latest_blockhash(rpc_url)
            raw = build_usdc_transfer_tx(
                sender=a_kp,
                recipient_pubkey=Pubkey.from_string(ch.b_pubkey),
                amount_atoms=a_atoms,
                priority_micro_lamports=fees.priority_micro_lamports,
                tip_lamports=fees.tip_lamports,
                tip_account=Pubkey.from_string(fees.tip_account),
                blockhash=blockhash,
            )
            txid = send_via_sender(sender_url, base64.b64encode(raw).decode())
            ch.stage3.txid = txid
            ch.stage3.status = "sent"
            save_state(state)
            logger.info(
                f"[stage3 chain {ch.chain_id:02d}] sent txid={txid} "
                f"USDC={a_atoms/(10**jup.USDC_DECIMALS):.4f} A->B={ch.b_pubkey}"
            )

            slot = confirm_signature(rpc_url, txid)
            if slot is None:
                ch.stage3.status = "failed"
                ch.stage3.error = "confirmation_timeout"
                logger.warning(f"[stage3 chain {ch.chain_id:02d}] timeout {txid}")
            else:
                ch.stage3.slot = slot
                ch.stage3.status = "confirmed"
                ch.stage3.completed_at_utc = now_utc()
                logger.info(f"[stage3 chain {ch.chain_id:02d}] confirmed slot={slot}")
            save_state(state)
        except Exception as e:
            ch.stage3.status = "failed"
            ch.stage3.error = str(e)
            save_state(state)
            logger.error(f"[stage3 chain {ch.chain_id:02d}] FAILED: {e}")

        if ch.chain_id < state.num_chains:
            sleep_jitter(rng, args, logger, f"stage3 chain {ch.chain_id:02d}")


# --------------------------------------------------------------------------- #
# Stage 4: B wallet swaps USDC -> SOL via Jupiter
# --------------------------------------------------------------------------- #
def run_stage4(
    state: MultiHopState,
    rpc_url: str,
    args: argparse.Namespace,
    logger: logging.Logger,
) -> None:
    rng = random.SystemRandom()

    for ch in state.chains:
        if ch.stage4.status == "confirmed":
            continue
        if ch.stage3.status != "confirmed":
            logger.warning(f"[stage4 chain {ch.chain_id:02d}] skipped (stage3 not confirmed)")
            continue
        try:
            b_kp = load_keypair_from(Path(ch.b_keypair_path))
            usdc_mint_pk = Pubkey.from_string(USDC_MINT)
            b_ata = str(get_associated_token_address(b_kp.pubkey(), usdc_mint_pk))
            b_atoms = get_token_balance_atoms(rpc_url, b_ata)
            if b_atoms <= 0:
                raise RuntimeError(f"B has 0 USDC atoms (ata={b_ata})")
            ch.stage4_input_atoms = b_atoms

            quote = jup.get_quote(
                input_mint=USDC_MINT,
                output_mint=SOL_MINT,
                amount=b_atoms,
                slippage_bps=args.slippage_bps,
            )
            est_out = jup.expected_out(quote)

            est_prio = get_priority_fee_estimate(rpc_url, [ch.b_pubkey]) or 0
            cu_price = max(args.prio_min, est_prio)
            unsigned = jup.build_swap_tx(
                quote=quote,
                user_pubkey=ch.b_pubkey,
                compute_unit_price_micro_lamports=cu_price,
            )
            signed = jup.sign_jupiter_tx(unsigned, b_kp)
            txid = send_via_rpc(rpc_url, base64.b64encode(signed).decode())
            ch.stage4.txid = txid
            ch.stage4.status = "sent"
            save_state(state)
            logger.info(
                f"[stage4 chain {ch.chain_id:02d}] sent txid={txid} "
                f"in={b_atoms/(10**jup.USDC_DECIMALS):.4f} USDC "
                f"quoted_out={est_out/LAMPORTS_PER_SOL:.6f} SOL"
            )

            slot = confirm_signature(rpc_url, txid)
            if slot is None:
                ch.stage4.status = "failed"
                ch.stage4.error = "confirmation_timeout"
                logger.warning(f"[stage4 chain {ch.chain_id:02d}] timeout {txid}")
                save_state(state)
                continue
            ch.stage4_output_lamports = get_balance_lamports(rpc_url, ch.b_pubkey)
            ch.stage4.slot = slot
            ch.stage4.status = "confirmed"
            ch.stage4.completed_at_utc = now_utc()
            logger.info(
                f"[stage4 chain {ch.chain_id:02d}] confirmed slot={slot} "
                f"B SOL balance={ch.stage4_output_lamports/LAMPORTS_PER_SOL:.6f}"
            )
            save_state(state)
        except Exception as e:
            ch.stage4.status = "failed"
            ch.stage4.error = str(e)
            save_state(state)
            logger.error(f"[stage4 chain {ch.chain_id:02d}] FAILED: {e}")

        if ch.chain_id < state.num_chains:
            sleep_jitter(rng, args, logger, f"stage4 chain {ch.chain_id:02d}")


# --------------------------------------------------------------------------- #
# Stage 5: B -> 5 final wallets (SOL transfer via Sender, randomized amounts)
# --------------------------------------------------------------------------- #
def run_stage5(
    state: MultiHopState,
    rpc_url: str,
    sender_url: str,
    min_tip: int,
    args: argparse.Namespace,
    logger: logging.Logger,
) -> None:
    rng = random.SystemRandom()

    for ch in state.chains:
        if all(d.op.status == "confirmed" for d in ch.stage5_distributions) and ch.stage5_distributions:
            continue
        if ch.stage4.status != "confirmed":
            logger.warning(f"[stage5 chain {ch.chain_id:02d}] skipped (stage4 not confirmed)")
            continue

        b_kp = load_keypair_from(Path(ch.b_keypair_path))

        # Allocate randomized amounts for any not-yet-allocated distributions.
        b_balance = get_balance_lamports(rpc_url, ch.b_pubkey)
        # leave 0.0001 SOL for last-tx fees + tip
        spendable = max(0, b_balance - 100_000)
        if not ch.stage5_distributions:
            ch.stage5_distributions = [
                FinalDistribution(final_pubkey=fp) for fp in ch.final_pubkeys
            ]
        # Allocate amounts (only for distributions still pending and 0)
        unallocated = [d for d in ch.stage5_distributions if d.amount_lamports == 0 and d.op.status != "confirmed"]
        already_allocated = sum(d.amount_lamports for d in ch.stage5_distributions if d.amount_lamports > 0)
        budget = spendable - already_allocated
        if unallocated:
            # randomize each between min and max, last gets remainder
            min_l = int(args.final_min_sol * LAMPORTS_PER_SOL)
            max_l = int(args.final_max_sol * LAMPORTS_PER_SOL)
            for i, d in enumerate(unallocated):
                if i < len(unallocated) - 1:
                    a = rng.randint(min_l, max_l)
                    a = min(a, max(min_l, budget // max(1, (len(unallocated) - i))))
                    d.amount_lamports = a
                    budget -= a
                else:
                    d.amount_lamports = max(0, budget)
            save_state(state)

        for dist in ch.stage5_distributions:
            if dist.op.status == "confirmed":
                continue
            if dist.amount_lamports <= 0:
                dist.op.status = "failed"
                dist.op.error = "zero_amount"
                save_state(state)
                continue
            try:
                fees = jitter_fees(rng, rpc_url, ch.b_pubkey, args, min_tip)
                blockhash = get_latest_blockhash(rpc_url)
                raw = build_sol_transfer_tx(
                    payer=b_kp,
                    recipient=Pubkey.from_string(dist.final_pubkey),
                    amount_lamports=dist.amount_lamports,
                    priority_micro_lamports=fees.priority_micro_lamports,
                    tip_lamports=fees.tip_lamports,
                    tip_account=Pubkey.from_string(fees.tip_account),
                    blockhash=blockhash,
                )
                txid = send_via_sender(sender_url, base64.b64encode(raw).decode())
                dist.op.txid = txid
                dist.op.status = "sent"
                save_state(state)
                logger.info(
                    f"[stage5 chain {ch.chain_id:02d}] sent txid={txid} "
                    f"{dist.amount_lamports/LAMPORTS_PER_SOL:.6f} SOL -> {dist.final_pubkey}"
                )
                slot = confirm_signature(rpc_url, txid)
                if slot is None:
                    dist.op.status = "failed"
                    dist.op.error = "confirmation_timeout"
                    logger.warning(f"[stage5 chain {ch.chain_id:02d}] timeout {txid}")
                else:
                    dist.op.slot = slot
                    dist.op.status = "confirmed"
                    dist.op.completed_at_utc = now_utc()
                    logger.info(f"[stage5 chain {ch.chain_id:02d}] confirmed slot={slot}")
                save_state(state)
            except Exception as e:
                dist.op.status = "failed"
                dist.op.error = str(e)
                save_state(state)
                logger.error(f"[stage5 chain {ch.chain_id:02d}] FAILED for {dist.final_pubkey}: {e}")

            sleep_jitter(rng, args, logger, f"stage5 chain {ch.chain_id:02d}")


# --------------------------------------------------------------------------- #
# Initialization (allocate fresh wallets + map finals to chains)
# --------------------------------------------------------------------------- #
def initialize_chains(
    state: MultiHopState,
    finals: list[str],
    num_chains: int,
    keygen_bin: str,
    logger: logging.Logger,
) -> None:
    if state.chains and len(state.chains) == num_chains:
        logger.info(f"reusing existing {num_chains} chains from state")
        return

    if state.chains:
        raise RuntimeError(
            f"existing state has {len(state.chains)} chains but --num-chains={num_chains}. "
            f"Delete {STATE_FILE} to start fresh."
        )

    if len(finals) % num_chains != 0:
        # Allow uneven splits: some chains get one extra
        logger.warning(
            f"{len(finals)} finals doesn't divide evenly into {num_chains} chains -- "
            "some chains will receive one more recipient"
        )
    base_size = len(finals) // num_chains
    extras = len(finals) % num_chains

    # Shuffle finals so chain<->final mapping is not deterministic from CSV order
    finals_shuffled = list(finals)
    random.SystemRandom().shuffle(finals_shuffled)

    cursor = 0
    chains: list[Chain] = []
    for i in range(num_chains):
        size = base_size + (1 if i < extras else 0)
        slice_pubkeys = finals_shuffled[cursor:cursor + size]
        cursor += size
        chains.append(Chain(
            chain_id=i + 1,
            final_pubkeys=slice_pubkeys,
        ))

    # Generate fresh wallets
    logger.info(f"generating {num_chains} A-wallets and {num_chains} B-wallets")
    for ch in chains:
        a_pubkey, a_path, _ = generate_fresh_wallet("A", ch.chain_id, keygen_bin)
        b_pubkey, b_path, _ = generate_fresh_wallet("B", ch.chain_id, keygen_bin)
        ch.a_pubkey = a_pubkey
        ch.b_pubkey = b_pubkey
        ch.a_keypair_path = str(a_path)
        ch.b_keypair_path = str(b_path)
        logger.info(
            f"chain {ch.chain_id:02d}: A={a_pubkey} B={b_pubkey} "
            f"finals={len(ch.final_pubkeys)}"
        )

    state.chains = chains
    state.num_chains = num_chains
    save_state(state)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Multi-hop Solana funder with Jupiter SOL<->USDC swap obfuscation.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--num-chains", type=int, default=DEFAULT_NUM_CHAINS,
                    help="Number of A/B wallet pairs (chains). 8 is the recommended default.")
    ap.add_argument("--treasury-per-chain-sol", type=float, default=DEFAULT_TREASURY_PER_CHAIN_SOL,
                    help="SOL per chain sent from treasury to A.")
    ap.add_argument("--final-min-sol", type=float, default=DEFAULT_FINAL_MIN_SOL)
    ap.add_argument("--final-max-sol", type=float, default=DEFAULT_FINAL_MAX_SOL)
    ap.add_argument("--min-delay", type=int, default=DEFAULT_MIN_DELAY)
    ap.add_argument("--max-delay", type=int, default=DEFAULT_MAX_DELAY)
    ap.add_argument("--prio-min", type=int, default=DEFAULT_PRIO_MIN)
    ap.add_argument("--prio-max", type=int, default=DEFAULT_PRIO_MAX)
    ap.add_argument("--slippage-bps", type=int, default=DEFAULT_SLIPPAGE_BPS)
    ap.add_argument("--dual", action="store_true",
                    help="Use dual-route Sender (Jito + validators) at 0.0002 SOL min tip "
                         "instead of SWQoS-only at 0.000005 SOL.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Initialize state and pre-flight only.")
    ap.add_argument("--only-stage", type=int, choices=[1, 2, 3, 4, 5], default=0,
                    help="Run only one stage (1-5). 0 = run all.")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE)

    api_key = os.environ.get("HELIUS_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write(
            "ERROR: HELIUS_API_KEY not set. Add it to .env or export it.\n"
            "Reuse the same key from ../funder/.env if you have one.\n"
        )
        return 6
    rpc_url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"
    sender_url = SENDER_ENDPOINT_DUAL if args.dual else SENDER_ENDPOINT_SWQOS
    min_tip = DUAL_MIN_TIP_LAMPORTS if args.dual else SWQOS_MIN_TIP_LAMPORTS

    if args.num_chains < 1 or args.num_chains > 40:
        sys.stderr.write("ERROR: --num-chains must be 1..40\n")
        return 2
    if args.final_min_sol <= 0 or args.final_max_sol < args.final_min_sol:
        sys.stderr.write("ERROR: invalid --final-min-sol / --final-max-sol\n")
        return 2

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    INTER_DIR.mkdir(parents=True, exist_ok=True)
    secure_chmod_dir(INTER_DIR)

    logger = setup_logging()
    keygen_bin = require_solana_keygen()
    treasury = load_treasury_keypair()
    treasury_pk = str(treasury.pubkey())
    finals = read_finals()
    logger.info(f"treasury  : {treasury_pk}")
    logger.info(f"finals    : {len(finals)} from {WALLETS_CSV}")
    logger.info(f"sender    : {sender_url}")
    logger.info(f"min tip   : {min_tip} lamports")

    state = load_state()
    if state is None:
        state = MultiHopState(
            started_at_utc=now_utc(),
            treasury_pubkey=treasury_pk,
            rpc_url=rpc_url,
            sender_endpoint=sender_url,
            num_chains=args.num_chains,
            chains=[],
        )
        save_state(state)
    else:
        if state.treasury_pubkey != treasury_pk:
            sys.stderr.write(
                f"ERROR: state file treasury mismatch ({state.treasury_pubkey} vs {treasury_pk}). "
                f"Delete {STATE_FILE} to start fresh.\n"
            )
            return 7

    initialize_chains(state, finals, args.num_chains, keygen_bin, logger)

    # Treasury balance pre-flight
    treasury_balance = get_balance_lamports(rpc_url, treasury_pk)
    needed = (
        int(args.treasury_per_chain_sol * LAMPORTS_PER_SOL) * state.num_chains
        + state.num_chains * (BASE_FEE_LAMPORTS + int(min_tip * 1.5) + 200_000)
        + 1_000_000
    )
    logger.info(
        f"treasury balance: {treasury_balance/LAMPORTS_PER_SOL:.6f} SOL  "
        f"need: {needed/LAMPORTS_PER_SOL:.6f} SOL"
    )
    if treasury_balance < needed:
        sys.stderr.write(
            f"ERROR: treasury underfunded. Have {treasury_balance/LAMPORTS_PER_SOL:.6f} SOL, "
            f"need {needed/LAMPORTS_PER_SOL:.6f}.\n"
        )
        return 8

    if args.dry_run:
        logger.info("DRY RUN: initialization complete, no transactions sent.")
        return 0

    only = args.only_stage
    if only in (0, 1):
        logger.info("====== STAGE 1: treasury -> A ======")
        run_stage1(state, treasury, rpc_url, sender_url, min_tip, args, logger)
    if only in (0, 2):
        logger.info("====== STAGE 2: A swap SOL -> USDC ======")
        run_stage2(state, rpc_url, args, logger)
    if only in (0, 3):
        logger.info("====== STAGE 3: A -> B (USDC) ======")
        run_stage3(state, rpc_url, sender_url, min_tip, args, logger)
    if only in (0, 4):
        logger.info("====== STAGE 4: B swap USDC -> SOL ======")
        run_stage4(state, rpc_url, args, logger)
    if only in (0, 5):
        logger.info("====== STAGE 5: B -> 40 finals ======")
        run_stage5(state, rpc_url, sender_url, min_tip, args, logger)

    # Summary
    s1 = sum(1 for c in state.chains if c.stage1.status == "confirmed")
    s2 = sum(1 for c in state.chains if c.stage2.status == "confirmed")
    s3 = sum(1 for c in state.chains if c.stage3.status == "confirmed")
    s4 = sum(1 for c in state.chains if c.stage4.status == "confirmed")
    s5_total = sum(len(c.stage5_distributions) for c in state.chains)
    s5_done = sum(
        1 for c in state.chains for d in c.stage5_distributions if d.op.status == "confirmed"
    )
    logger.info(
        f"summary: stage1={s1}/{state.num_chains}  stage2={s2}/{state.num_chains}  "
        f"stage3={s3}/{state.num_chains}  stage4={s4}/{state.num_chains}  "
        f"stage5={s5_done}/{s5_total}"
    )
    return 0 if (s1 == s2 == s3 == s4 == state.num_chains and s5_done == s5_total) else 1


if __name__ == "__main__":
    sys.exit(main())
