"""
Jupiter Aggregator swap client (v1).

Endpoint: https://api.jup.ag/swap/v1   (current production endpoint as of 2026,
the Jupiter Developer Platform unified gateway). The legacy `quote-api.jup.ag/v6`
still resolves but is being deprecated through 2026.

Free-tier rate limits: 1 RPS, 60 requests/min — well above what we need
(16 swaps total over ~20 minutes).

The flow is two HTTP calls:
    GET  /quote   -> quote response JSON
    POST /swap    -> base64-encoded UNSIGNED VersionedTransaction

The caller signs with the user keypair and submits. Jupiter does NOT need
to co-sign; the user is the only required signer for a swap.
"""

from __future__ import annotations

import base64
from typing import Optional

import requests
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction


JUPITER_API = "https://api.jup.ag/swap/v1"

# Common mints (mainnet-beta)
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

SOL_DECIMALS = 9
USDC_DECIMALS = 6

DEFAULT_SLIPPAGE_BPS = 100      # 1.00 %
DEFAULT_HTTP_TIMEOUT = 20


class JupiterError(Exception):
    pass


def get_quote(
    input_mint: str,
    output_mint: str,
    amount: int,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
    timeout: int = DEFAULT_HTTP_TIMEOUT,
) -> dict:
    """
    GET /v1/quote

    `amount` is in the smallest unit of `input_mint`
    (lamports for SOL, atoms for USDC).
    """
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": str(slippage_bps),
        "swapMode": "ExactIn",
        "asLegacyTransaction": "false",
        "onlyDirectRoutes": "false",
        "maxAccounts": "64",
    }
    r = requests.get(f"{JUPITER_API}/quote", params=params, timeout=timeout)
    if r.status_code != 200:
        raise JupiterError(f"quote {r.status_code}: {r.text[:500]}")
    j = r.json()
    if not isinstance(j, dict) or "outAmount" not in j:
        raise JupiterError(f"quote: malformed response: {j}")
    return j


def build_swap_tx(
    quote: dict,
    user_pubkey: str,
    compute_unit_price_micro_lamports: Optional[int] = None,
    timeout: int = DEFAULT_HTTP_TIMEOUT,
) -> bytes:
    """
    POST /v1/swap

    Returns the raw bytes of an UNSIGNED VersionedTransaction. The caller
    must sign with the user's keypair before submitting.
    """
    body: dict = {
        "quoteResponse": quote,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "useSharedAccounts": True,
        "asLegacyTransaction": False,
        "useTokenLedger": False,
    }
    if compute_unit_price_micro_lamports is not None:
        body["computeUnitPriceMicroLamports"] = int(compute_unit_price_micro_lamports)

    r = requests.post(
        f"{JUPITER_API}/swap",
        json=body,
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    if r.status_code != 200:
        raise JupiterError(f"swap {r.status_code}: {r.text[:500]}")
    j = r.json()
    swap_tx_b64 = j.get("swapTransaction")
    if not swap_tx_b64:
        raise JupiterError(f"swap: missing swapTransaction in response: {j}")
    return base64.b64decode(swap_tx_b64)


def sign_jupiter_tx(raw_tx_bytes: bytes, signer: Keypair) -> bytes:
    """
    Deserialize Jupiter's unsigned VersionedTransaction, re-sign with the
    user keypair, return signed wire bytes ready for `sendTransaction`.

    Jupiter returns a tx whose only required signer is `userPublicKey`.
    We rebuild the VersionedTransaction with our keypair as the signer,
    which produces the canonical signed wire format.
    """
    tx = VersionedTransaction.from_bytes(raw_tx_bytes)
    signed = VersionedTransaction(tx.message, [signer])
    return bytes(signed)


def expected_out(quote: dict) -> int:
    """Quoted output amount in smallest units of `outputMint`."""
    return int(quote["outAmount"])


def min_out_after_slippage(quote: dict) -> int:
    """Quoted minimum output after slippage (otherMinAmount or computed)."""
    if "otherAmountThreshold" in quote:
        return int(quote["otherAmountThreshold"])
    return int(quote["outAmount"])
