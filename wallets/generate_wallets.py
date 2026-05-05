#!/usr/bin/env python3
"""
Production-grade Solana batch wallet generator.

What it does
------------
- Invokes the official `solana-keygen new` binary (Agave / anza-xyz) to generate
  each keypair locally, with no BIP39 passphrase, capturing the printed
  12-word BIP39 seed phrase from stdout.
- For each wallet, picks a base proxy endpoint from `proxies.txt` (round-robin)
  and injects a unique sticky-session id into the username so wallet N is
  bound to a specific residential exit IP for its entire lifetime. Compatible
  with Smartproxy/Decodo, Bright Data, Oxylabs, IPRoyal and any provider that
  follows the `user-session-XXXX:pass@host:port` convention.
- Verifies the actual exit IP through each sticky-session URL via
  https://api.ipify.org BEFORE assigning, and records that real IP.
- Writes:
    * private/<pubkey>.json          - solana-keygen Vec<u8> keypair (chmod 600)
    * private/<pubkey>.seed.txt      - 12-word BIP39 mnemonic (chmod 600)
    * public/master_pubkeys.txt      - one public key per line
    * wallets.csv                    - full master CSV (pubkey, secret, seed,
                                       paths, proxy, session id, exit ip,
                                       creation timestamp UTC)
    * logs/creation.log              - per-wallet creation log (UTC)
- Sleeps a random number of seconds between each wallet (default 5-60s).

Honest scope note
-----------------
`solana-keygen new` is a 100% local, offline cryptographic operation. No
packets leave the machine during key creation. The proxy system here is
therefore a *tagging* layer: each wallet is permanently bound to a specific
residential exit IP so that ALL FUTURE on-chain activity from that wallet
(funding from a CEX, RPC calls, dApp logins) goes through the same residential
IP and never cross-contaminates with another wallet's IP. The randomized
delay still mimics human pacing for any logging/observers that watch the
filesystem or a future wrapper service.

Designed for Ubuntu 22.04+ production deployment. Cross-platform (works on
Windows for development; chmod is a best-effort no-op on Windows).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import platform
import random
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import base58
    import requests
except ImportError as e:
    sys.stderr.write(
        f"ERROR: missing dependency ({e.name}). "
        "Run: pip install -r requirements.txt\n"
    )
    sys.exit(2)


# --------------------------------------------------------------------------- #
# Paths & constants
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent
PRIVATE_DIR = ROOT / "private"
PUBLIC_DIR = ROOT / "public"
LOGS_DIR = ROOT / "logs"
MASTER_PUBKEYS = PUBLIC_DIR / "master_pubkeys.txt"
WALLETS_CSV = ROOT / "wallets.csv"
PROXIES_FILE = ROOT / "proxies.txt"
CREATION_LOG = LOGS_DIR / "creation.log"

IP_CHECK_URL = "https://api.ipify.org?format=json"
IP_CHECK_TIMEOUT = 15  # seconds

DEFAULT_COUNT = 40
DEFAULT_MIN_DELAY = 5
DEFAULT_MAX_DELAY = 60

CSV_HEADER = [
    "index",
    "created_at_utc",
    "pubkey",
    "secret_key_b58",
    "seed_phrase_12w",
    "keypair_json_path",
    "seed_phrase_path",
    "proxy_endpoint",
    "proxy_session_id",
    "proxy_exit_ip",
]


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def setup_logging() -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("wallet_gen")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter(
        fmt="%(asctime)sZ | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    fmt.converter = time.gmtime
    fh = logging.FileHandler(CREATION_LOG, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


# --------------------------------------------------------------------------- #
# Pre-flight
# --------------------------------------------------------------------------- #
def require_solana_keygen() -> str:
    path = shutil.which("solana-keygen")
    if path:
        return path
    sys.stderr.write(
        "ERROR: `solana-keygen` not found on PATH.\n"
        "  Ubuntu/Linux: bash setup.sh\n"
        "  Windows:      see README.md (Install section)\n"
    )
    sys.exit(3)


def secure_chmod(p: Path) -> None:
    """Best-effort owner-only rw on POSIX. No-op on Windows / DrvFs."""
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def secure_chmod_dir(p: Path) -> None:
    """Best-effort owner-only rwx on POSIX."""
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Proxy handling
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ProxyAssignment:
    base_endpoint: str
    session_id: str
    sticky_url: str
    exit_ip: Optional[str]


def load_proxies() -> list[str]:
    if not PROXIES_FILE.exists():
        return []
    out: list[str] = []
    for line in PROXIES_FILE.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def inject_session(proxy_url: str, session_id: str) -> str:
    """
    Inject a sticky-session id into the proxy username, following the
    convention used by Smartproxy/Decodo, Bright Data, Oxylabs and IPRoyal:

        scheme://user-session-XXXX:pass@host:port

    If the URL has no auth section, returns it unchanged (no-op for
    static-IP proxies that do not support session control).
    """
    if "://" not in proxy_url:
        return proxy_url
    scheme, rest = proxy_url.split("://", 1)
    if "@" not in rest:
        return proxy_url
    auth, host = rest.rsplit("@", 1)
    if ":" in auth:
        user, pw = auth.split(":", 1)
    else:
        user, pw = auth, ""
    new_user = f"{user}-session-{session_id}"
    if pw:
        return f"{scheme}://{new_user}:{pw}@{host}"
    return f"{scheme}://{new_user}@{host}"


def verify_exit_ip(proxy_url: str, logger: logging.Logger) -> Optional[str]:
    proxies = {"http": proxy_url, "https": proxy_url}
    try:
        r = requests.get(IP_CHECK_URL, proxies=proxies, timeout=IP_CHECK_TIMEOUT)
        r.raise_for_status()
        ip = r.json().get("ip")
        if not ip:
            return None
        return str(ip)
    except Exception as e:
        host = proxy_url.rsplit("@", 1)[-1] if "@" in proxy_url else proxy_url
        logger.warning(f"Proxy IP verification failed for {host}: {e}")
        return None


def assign_proxy(
    idx: int,
    base_proxies: list[str],
    verify: bool,
    logger: logging.Logger,
) -> ProxyAssignment:
    if not base_proxies:
        return ProxyAssignment(
            base_endpoint="NONE",
            session_id=f"placeholder-{idx:03d}",
            sticky_url="",
            exit_ip=None,
        )
    base = base_proxies[(idx - 1) % len(base_proxies)]
    session_id = f"w{idx:03d}-{random.randint(10_000, 99_999)}"
    sticky = inject_session(base, session_id)
    exit_ip = verify_exit_ip(sticky, logger) if verify else None
    return ProxyAssignment(
        base_endpoint=base,
        session_id=session_id,
        sticky_url=sticky,
        exit_ip=exit_ip,
    )


# --------------------------------------------------------------------------- #
# solana-keygen wrapper
# --------------------------------------------------------------------------- #
def run_solana_keygen(out_json: Path, keygen_bin: str) -> dict:
    """
    Invoke `solana-keygen new` non-interactively and capture stdout to extract
    the pubkey and the 12-word BIP39 seed phrase.

    NOTE: We deliberately DO NOT pass `--silent` here; --silent suppresses the
    seed phrase output, which we need to record. --no-bip39-passphrase still
    skips the only interactive prompt, so this is fully scripted.
    """
    cmd = [
        keygen_bin, "new",
        "--no-bip39-passphrase",
        "--force",
        "--outfile", str(out_json),
    ]
    proc = subprocess.run(
        cmd,
        input="",
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"solana-keygen exited {proc.returncode}: "
            f"stderr={proc.stderr.strip()!r}"
        )

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
        raise RuntimeError(
            "Failed to parse solana-keygen output.\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}"
        )

    raw = json.loads(out_json.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or len(raw) != 64:
        raise RuntimeError(f"Unexpected keypair JSON shape in {out_json}")
    secret_b58 = base58.b58encode(bytes(raw)).decode()

    return {
        "pubkey": pubkey,
        "seed_phrase": seed_phrase,
        "secret_key_b58": secret_b58,
    }


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Production Solana batch wallet generator.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("-n", "--count", type=int, default=DEFAULT_COUNT,
                    help="Number of wallets to generate")
    ap.add_argument("--min-delay", type=int, default=DEFAULT_MIN_DELAY,
                    help="Min delay between wallets (seconds)")
    ap.add_argument("--max-delay", type=int, default=DEFAULT_MAX_DELAY,
                    help="Max delay between wallets (seconds)")
    ap.add_argument("--no-proxy-verify", action="store_true",
                    help="Skip ipify exit-IP verification (faster, less safe)")
    ap.add_argument("--seed", type=int, default=None,
                    help="Optional RNG seed for delays/session ids (testing only)")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    if args.count < 1:
        sys.stderr.write("ERROR: --count must be >= 1\n")
        return 2
    if args.min_delay < 0 or args.max_delay < args.min_delay:
        sys.stderr.write("ERROR: invalid --min-delay / --max-delay\n")
        return 2
    if args.seed is not None:
        random.seed(args.seed)

    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    secure_chmod_dir(PRIVATE_DIR)

    logger = setup_logging()
    keygen_bin = require_solana_keygen()
    logger.info(f"solana-keygen: {keygen_bin}")
    logger.info(f"platform     : {platform.system()} {platform.release()}")

    proxies = load_proxies()
    if proxies:
        logger.info(f"loaded {len(proxies)} base proxy endpoint(s) from proxies.txt")
    else:
        logger.warning(
            "proxies.txt not found or empty -> using placeholder proxy ids. "
            "(Reminder: solana-keygen creation is fully local; proxy assignment "
            "is a permanent tag for FUTURE on-chain use of each wallet.)"
        )

    csv_exists = WALLETS_CSV.exists()
    csv_fh = WALLETS_CSV.open("a", newline="", encoding="utf-8")
    writer = csv.writer(csv_fh)
    if not csv_exists:
        writer.writerow(CSV_HEADER)
        csv_fh.flush()

    pubkeys_fh = MASTER_PUBKEYS.open("a", encoding="utf-8")

    logger.info(
        f"generating {args.count} wallets, delay range "
        f"[{args.min_delay}s, {args.max_delay}s], "
        f"proxy_verify={'on' if (proxies and not args.no_proxy_verify) else 'off'}"
    )

    successes = 0
    try:
        for i in range(1, args.count + 1):
            try:
                proxy = assign_proxy(
                    idx=i,
                    base_proxies=proxies,
                    verify=(not args.no_proxy_verify) and bool(proxies),
                    logger=logger,
                )

                tmp_json = PRIVATE_DIR / f".tmp_wallet_{i:03d}.json"
                result = run_solana_keygen(tmp_json, keygen_bin)
                pubkey = result["pubkey"]

                final_json = PRIVATE_DIR / f"{pubkey}.json"
                tmp_json.replace(final_json)
                secure_chmod(final_json)

                seed_path = PRIVATE_DIR / f"{pubkey}.seed.txt"
                seed_path.write_text(result["seed_phrase"] + "\n", encoding="utf-8")
                secure_chmod(seed_path)

                created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                writer.writerow([
                    i,
                    created_at,
                    pubkey,
                    result["secret_key_b58"],
                    result["seed_phrase"],
                    str(final_json),
                    str(seed_path),
                    proxy.base_endpoint,
                    proxy.session_id,
                    proxy.exit_ip or "UNVERIFIED",
                ])
                csv_fh.flush()
                pubkeys_fh.write(pubkey + "\n")
                pubkeys_fh.flush()

                logger.info(
                    f"[{i:02d}/{args.count}] pubkey={pubkey} "
                    f"session={proxy.session_id} "
                    f"exit_ip={proxy.exit_ip or 'UNVERIFIED'}"
                )
                successes += 1
            except Exception as e:
                logger.error(f"[{i:02d}/{args.count}] FAILED: {e}")
                continue

            if i < args.count:
                d = random.uniform(args.min_delay, args.max_delay)
                logger.info(f"sleep {d:.1f}s before next wallet")
                time.sleep(d)
    finally:
        csv_fh.close()
        pubkeys_fh.close()

    logger.info(
        f"done. {successes}/{args.count} wallets created. "
        f"csv={WALLETS_CSV}  pubkeys={MASTER_PUBKEYS}  log={CREATION_LOG}"
    )
    return 0 if successes == args.count else 1


if __name__ == "__main__":
    sys.exit(main())
