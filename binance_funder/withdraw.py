"""
Option B: split stealth inflow across two CEX hot-wallet pools.

  * Wallets sorted by CSV `index` (numeric): first OKX_WALLET_COUNT -> OKX,
    remainder -> Bybit (default 24 + 16 = 40).

Uses raw REST + HMAC (no exchange SDKs) so dependency surface stays small.

OKX:  POST https://www.okx.com/api/v5/asset/withdrawal
Bybit: POST https://api.bybit.com/v5/asset/withdraw/create

Requires API keys with withdrawal permission and (Bybit) whitelisted addresses.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

# --- Credentials (all from .env) ---
OKX_API_KEY = os.getenv("OKX_API_KEY", "").strip()
OKX_API_SECRET = os.getenv("OKX_API_SECRET", "").strip()
OKX_API_PASSPHRASE = os.getenv("OKX_API_PASSPHRASE", "").strip()

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "").strip()
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "").strip()

# Split: first N wallets (by sorted index) use OKX, rest use Bybit.
OKX_WALLET_COUNT = int(os.getenv("OKX_WALLET_COUNT", "24"))
BYBIT_WALLET_COUNT = int(os.getenv("BYBIT_WALLET_COUNT", "16"))

# Optional overrides (otherwise OKX chain+fee are resolved from public /asset/currencies).
OKX_SOL_CHAIN = os.getenv("OKX_SOL_CHAIN", "").strip()
OKX_SOL_FEE = os.getenv("OKX_SOL_FEE", "").strip()
BYBIT_SOL_CHAIN = os.getenv("BYBIT_SOL_CHAIN", "SOL").strip()

OKX_API_BASE = os.getenv("OKX_API_BASE", "https://www.okx.com").rstrip("/")
BYBIT_API_BASE = os.getenv("BYBIT_API_BASE", "https://api.bybit.com").rstrip("/")

# Paths
WALLETS_CSV = "../wallets/wallets.csv"
STATE_DIR = "state"
STATE_FILE = f"{STATE_DIR}/funding_state.json"
LOG_DIR = "logs"
LOG_FILE = f"{LOG_DIR}/funding.log"

# ~1 SOL total notional across 40 wallets (gross amount requested per withdrawal;
# exchange network fee is deducted from your CEX balance on top of `amt`).
MIN_WITHDRAW_SOL = 0.024
MAX_WITHDRAW_SOL = 0.026

# Temporal spacing between any two withdrawals (same script = same machine IP).
MIN_DELAY_SEC = 120
MAX_DELAY_SEC = 900

# Bybit: max 1 withdraw / 10 s per (coin, chain) — stay under that between Bybit calls.
BYBIT_MIN_SPACING_SEC = 11.0
_last_bybit_withdraw_mono: float = 0.0  # time.monotonic() for Bybit 10s spacing rule


def log(msg: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state() -> dict[str, Any]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def load_wallets() -> list[dict[str, str]]:
    if not os.path.exists(WALLETS_CSV):
        print(f"ERROR: Wallets file {WALLETS_CSV} not found.")
        sys.exit(1)
    wallets: list[dict[str, str]] = []
    with open(WALLETS_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            wallets.append({"index": row["index"], "pubkey": row["pubkey"]})
    wallets.sort(key=lambda w: int(w["index"]))
    return wallets


def _okx_timestamp() -> str:
    now = datetime.now(timezone.utc)
    ms = now.microsecond // 1000
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms:03d}Z"


def _okx_sign(timestamp: str, method: str, path: str, body: str, secret: str) -> str:
    prehash = f"{timestamp}{method.upper()}{path}{body}"
    mac = hmac.new(secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("ascii")


def _okx_request(
    method: str,
    path: str,
    body: dict[str, Any] | None,
    session: requests.Session,
) -> dict[str, Any]:
    body_s = "" if body is None else json.dumps(body, separators=(",", ":"), sort_keys=True)
    ts = _okx_timestamp()
    sign = _okx_sign(ts, method, path, body_s, OKX_API_SECRET)
    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_API_PASSPHRASE,
        "Content-Type": "application/json",
    }
    url = f"{OKX_API_BASE}{path}"
    if method.upper() == "GET":
        r = session.request(method, url, headers=headers, timeout=60)
    else:
        r = session.request(
            method, url, headers=headers, data=body_s.encode("utf-8"), timeout=60
        )
    try:
        return r.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(f"OKX non-JSON response HTTP {r.status_code}: {r.text[:500]}") from e


def fetch_okx_sol_meta(session: requests.Session) -> tuple[str, str, str]:
    """Return (chain, fee, minWd) for SOL on-chain withdraw from OKX public API."""
    if OKX_SOL_CHAIN and OKX_SOL_FEE:
        min_wd = os.getenv("OKX_MIN_WD", "0.01")
        return OKX_SOL_CHAIN, OKX_SOL_FEE, min_wd
    r = session.get(
        f"{OKX_API_BASE}/api/v5/asset/currencies",
        params={"ccy": "SOL"},
        timeout=60,
    )
    r.raise_for_status()
    j = r.json()
    if j.get("code") != "0":
        raise RuntimeError(f"OKX currencies error: {j}")
    rows: list[dict[str, Any]] = j.get("data") or []
    sol_rows = [x for x in rows if x.get("ccy") == "SOL"]
    pick: dict[str, Any] | None = None
    for x in sol_rows:
        chain = str(x.get("chain") or "")
        if chain.endswith("-Solana") or chain == "SOL-Solana":
            if x.get("canWd") in (True, "true", "1", 1):
                pick = x
                break
    if pick is None:
        for x in sol_rows:
            if x.get("canWd") in (True, "true", "1", 1):
                pick = x
                break
    if pick is None:
        raise RuntimeError("OKX /asset/currencies: no withdrawable SOL row found.")
    chain = str(pick["chain"])
    fee = str(pick.get("minFee") or pick.get("fee") or "0")
    min_wd = str(pick.get("minWd") or "0")
    return chain, fee, min_wd


def okx_withdraw(
    session: requests.Session,
    to_addr: str,
    amount: float,
    chain: str,
    fee: str,
) -> str:
    amt_s = f"{amount:.8f}".rstrip("0").rstrip(".")
    body: dict[str, Any] = {
        "amt": amt_s,
        "ccy": "SOL",
        "chain": chain,
        "dest": "4",
        "fee": fee,
        "toAddr": to_addr,
    }
    j = _okx_request("POST", "/api/v5/asset/withdrawal", body, session)
    if j.get("code") != "0":
        raise RuntimeError(f"OKX withdrawal rejected: {j}")
    data = j.get("data") or []
    if not data:
        raise RuntimeError(f"OKX withdrawal empty data: {j}")
    wd_id = str(data[0].get("wdId") or data[0].get("clientId") or "unknown")
    return wd_id


def _bybit_sign(api_secret: str, payload: str) -> str:
    return hmac.new(
        api_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def bybit_withdraw(
    session: requests.Session,
    to_addr: str,
    amount: float,
    chain: str,
) -> str:
    global _last_bybit_withdraw_mono
    now = time.monotonic()
    wait = BYBIT_MIN_SPACING_SEC - (now - _last_bybit_withdraw_mono)
    if wait > 0:
        log(f"  -> Bybit spacing: sleeping {wait:.1f}s (exchange limit: 1 withdraw / 10s per coin+chain).")
        time.sleep(wait)
    ts = str(int(time.time() * 1000))
    recv_window = "5000"
    amt_s = f"{amount:.8f}".rstrip("0").rstrip(".")
    body_obj: dict[str, Any] = {
        "accountType": "FUND",
        "address": to_addr,
        "amount": amt_s,
        "coin": "SOL",
        "forceChain": 1,
        "chain": chain,
        "timestamp": int(ts),
    }
    body_raw = json.dumps(body_obj, separators=(",", ":"), sort_keys=True)
    sign_payload = f"{ts}{BYBIT_API_KEY}{recv_window}{body_raw}"
    sign = _bybit_sign(BYBIT_API_SECRET, sign_payload)
    headers = {
        "Content-Type": "application/json",
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN": sign,
    }
    url = f"{BYBIT_API_BASE}/v5/asset/withdraw/create"
    r = session.post(url, headers=headers, data=body_raw.encode("utf-8"), timeout=60)
    try:
        j = r.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Bybit non-JSON HTTP {r.status_code}: {r.text[:500]}") from e
    if j.get("retCode") != 0:
        raise RuntimeError(f"Bybit withdrawal rejected: {j}")
    wid = str((j.get("result") or {}).get("id") or "unknown")
    _last_bybit_withdraw_mono = time.monotonic()
    return wid


def assign_exchange(row_rank: int) -> str:
    """row_rank = 0-based position after sorting wallets by `index` ascending."""
    if row_rank < OKX_WALLET_COUNT:
        return "okx"
    return "bybit"


def main() -> None:
    missing_okx = not (OKX_API_KEY and OKX_API_SECRET and OKX_API_PASSPHRASE)
    missing_bybit = not (BYBIT_API_KEY and BYBIT_API_SECRET)
    if missing_okx or missing_bybit:
        log(
            "ERROR: Set OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE, "
            "BYBIT_API_KEY, and BYBIT_API_SECRET in .env (see .env.example)."
        )
        sys.exit(1)

    if OKX_WALLET_COUNT < 0 or BYBIT_WALLET_COUNT < 0:
        log("ERROR: OKX_WALLET_COUNT and BYBIT_WALLET_COUNT must be >= 0.")
        sys.exit(1)

    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    wallets = load_wallets()
    n = len(wallets)
    if n == 0:
        log("ERROR: No wallets in wallets.csv.")
        sys.exit(1)
    if OKX_WALLET_COUNT + BYBIT_WALLET_COUNT != n:
        log(
            f"ERROR: OKX_WALLET_COUNT ({OKX_WALLET_COUNT}) + BYBIT_WALLET_COUNT "
            f"({BYBIT_WALLET_COUNT}) must equal wallet rows ({n}). "
            f"Fix .env or regenerate {n} wallets."
        )
        sys.exit(1)

    state = load_state()
    log(
        f"Loaded {n} wallets (sorted by CSV index). Split by row order: "
        f"first {OKX_WALLET_COUNT} rows -> OKX, next {BYBIT_WALLET_COUNT} -> Bybit."
    )

    session = requests.Session()

    okx_chain, okx_fee, okx_min_wd = fetch_okx_sol_meta(session)
    log(f"OKX SOL metadata: chain={okx_chain!r} fee={okx_fee!r} minWd={okx_min_wd!r}")
    min_wd_f = float(okx_min_wd)
    if MAX_WITHDRAW_SOL < min_wd_f:
        log(f"ERROR: MAX_WITHDRAW_SOL ({MAX_WITHDRAW_SOL}) < OKX minWd ({min_wd_f}).")
        sys.exit(1)

    log("Starting withdrawal loop (random amounts + random delays).")

    for i, w in enumerate(wallets):
        pubkey = w["pubkey"]
        w_index = int(w["index"])
        ex = assign_exchange(i)

        if pubkey in state and state[pubkey].get("status") == "withdrawn":
            log(f"[{i + 1}/{n}] Wallet {w_index} ({pubkey}) [{ex}] already funded. Skipping.")
            continue

        amount = round(random.uniform(MIN_WITHDRAW_SOL, MAX_WITHDRAW_SOL), 6)
        log(
            f"[{i + 1}/{n}] Wallet {w_index} ({pubkey}) via {ex.upper()}: "
            f"withdrawing {amount} SOL (notional)..."
        )

        try:
            if ex == "okx":
                wd_id = okx_withdraw(session, pubkey, amount, okx_chain, okx_fee)
            else:
                wd_id = bybit_withdraw(session, pubkey, amount, BYBIT_SOL_CHAIN)

            log(f"  -> Success. Withdrawal id: {wd_id}")

            state[pubkey] = {
                "index": w_index,
                "exchange": ex,
                "amount": amount,
                "status": "withdrawn",
                "withdraw_id": wd_id,
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            save_state(state)

        except Exception as e:
            log(f"  -> ERROR: {e}")
            log("Aborting to prevent partial or broken state. Fix the error and re-run.")
            sys.exit(1)

        if i < n - 1:
            delay = random.randint(MIN_DELAY_SEC, MAX_DELAY_SEC)
            log(f"Sleeping {delay}s to reduce temporal clustering...")
            time.sleep(delay)

    log("Funding complete. All wallets queued for withdrawal (OKX + Bybit).")


if __name__ == "__main__":
    main()
