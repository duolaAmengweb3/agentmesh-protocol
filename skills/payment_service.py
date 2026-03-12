"""x402 Solana payment service — real USDC escrow via CDP facilitator.

x402 flow:
  1. Client POSTs to create bounty (no X-PAYMENT header)
  2. Server returns 402 with payment requirements (USDC amount, payTo wallet)
  3. Client signs Solana SPL transfer, retries with X-PAYMENT header
  4. Server verifies via CDP facilitator
  5. Server creates bounty, then settles payment via CDP

Money flow:
  A posts bounty (100 USDC) → 100 locked (paid to platform wallet)
  B delivers → A accepts → B gets 95, platform keeps 5
  A cancels (never delivered) → A gets 100 back
  A cancels (after delivery) → A gets 95 back, platform keeps 5

Falls back to simulated escrow when platform_wallet_address is not configured.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.bounty import Bounty

logger = logging.getLogger(__name__)

# Solana network CAIP-2 identifiers
SOLANA_NETWORKS = {
    "devnet": "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
    "mainnet": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
}

# USDC decimals on Solana
USDC_DECIMALS = 6


def _is_x402_configured() -> bool:
    """Check if real x402 payment is configured."""
    return bool(
        settings.x402_cdp_api_key
        and settings.x402_cdp_api_secret
        and settings.platform_wallet_address
    )


def _usd_to_usdc_atomic(amount: float) -> str:
    """Convert USD amount to USDC smallest units (6 decimals). 1 USDC ≈ 1 USD."""
    return str(int(amount * (10 ** USDC_DECIMALS)))


def _create_cdp_jwt(uri: str) -> str:
    """Create a JWT for CDP API authentication.

    Uses Ed25519 (EdDSA) signing with the CDP API secret.
    """
    import jwt  # PyJWT

    key_bytes = base64.b64decode(settings.x402_cdp_api_secret)
    # Ed25519 keypair: first 32 bytes = seed, last 32 bytes = public key
    seed = key_bytes[:32]
    private_key = Ed25519PrivateKey.from_private_bytes(seed)

    now = int(time.time())
    header = {
        "alg": "EdDSA",
        "kid": settings.x402_cdp_api_key,
        "nonce": secrets.token_hex(16),
        "typ": "JWT",
    }
    payload = {
        "sub": settings.x402_cdp_api_key,
        "iss": "cdp",
        "aud": ["cdp_service"],
        "nbf": now,
        "exp": now + 120,
        "uris": [uri],
    }
    return jwt.encode(payload, private_key, algorithm="EdDSA", headers=header)


def _get_network_id() -> str:
    """Get Solana network CAIP-2 identifier."""
    return SOLANA_NETWORKS.get(settings.solana_network, SOLANA_NETWORKS["devnet"])


# ---------------------------------------------------------------------------
# Payment Requirements (402 response)
# ---------------------------------------------------------------------------

def build_payment_requirements(amount: float, resource: str, description: str = "") -> dict:
    """Build x402 payment requirements for a 402 response.

    Returns the payload that goes in the 402 response body and
    X-Payment-Requirements header.
    """
    return {
        "x402Version": 1,
        "accepts": [
            {
                "scheme": "exact",
                "network": _get_network_id(),
                "maxAmountRequired": _usd_to_usdc_atomic(amount),
                "resource": resource,
                "description": description or f"Payment of {amount} USDC",
                "mimeType": "application/json",
                "payTo": settings.platform_wallet_address,
                "requiredDeadlineSeconds": 300,
                "extra": {
                    "token": settings.usdc_mint_address,
                },
            }
        ],
    }


# ---------------------------------------------------------------------------
# x402 Verification & Settlement via CDP Facilitator
# ---------------------------------------------------------------------------

async def verify_x402_payment(x_payment_header: str, payment_requirements: dict) -> dict:
    """Verify an X-PAYMENT header via CDP facilitator.

    Returns {"valid": True, "payment": <decoded>} on success.
    Raises ValueError on failure.
    """
    try:
        payment = json.loads(base64.b64decode(x_payment_header))
    except Exception as e:
        raise ValueError(f"Invalid X-PAYMENT header encoding: {e}")

    verify_url = f"{settings.x402_facilitator_url}/verify"
    token = _create_cdp_jwt(verify_url)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            verify_url,
            json={
                "x402Version": 1,
                "payment": payment,
                "paymentRequirements": payment_requirements,
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        if resp.status_code != 200:
            logger.error("CDP verify failed [%d]: %s", resp.status_code, resp.text)
            raise ValueError(f"Payment verification failed: {resp.text}")

        result = resp.json()
        if not result.get("valid", False):
            raise ValueError("Payment verification returned invalid")

        return {"valid": True, "payment": payment}


async def settle_x402_payment(x_payment_header: str, payment_requirements: dict) -> dict:
    """Settle (submit on-chain) a verified payment via CDP facilitator.

    Call this AFTER the bounty is created to finalize the on-chain transaction.
    """
    try:
        payment = json.loads(base64.b64decode(x_payment_header))
    except Exception:
        payment = x_payment_header  # Already decoded

    settle_url = f"{settings.x402_facilitator_url}/settle"
    token = _create_cdp_jwt(settle_url)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            settle_url,
            json={
                "x402Version": 1,
                "payment": payment,
                "paymentRequirements": payment_requirements,
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        if resp.status_code != 200:
            logger.error("CDP settle failed [%d]: %s", resp.status_code, resp.text)
            raise ValueError(f"Payment settlement failed: {resp.text}")

        return resp.json()


# ---------------------------------------------------------------------------
# Escrow operations (used by bounty lifecycle)
# ---------------------------------------------------------------------------

async def lock_escrow(
    session: AsyncSession,
    bounty: Bounty,
    x_payment_header: str | None = None,
    payment_requirements: dict | None = None,
) -> dict:
    """Lock funds in escrow when bounty is posted.

    If x402 is configured and x_payment_header is provided:
      - Verify payment via CDP facilitator
      - Settle the transaction on-chain
      - Store the tx signature as escrow_tx_id

    Otherwise: simulate (for dev/test).
    """
    logger.info("Locking escrow for bounty %s: %s %s", bounty.id, bounty.amount, bounty.currency)

    if _is_x402_configured() and x_payment_header and payment_requirements:
        # Real x402 flow
        try:
            verify_result = await verify_x402_payment(x_payment_header, payment_requirements)
            settle_result = await settle_x402_payment(x_payment_header, payment_requirements)

            tx_id = settle_result.get("txHash") or settle_result.get("transaction_hash", "")
            bounty.escrow_tx_id = tx_id or f"x402_{bounty.id}"
            bounty.currency = "USDC"

            logger.info("Escrow locked on-chain: tx=%s", bounty.escrow_tx_id)
            return {
                "status": "locked",
                "bounty_id": bounty.id,
                "tx_id": bounty.escrow_tx_id,
                "network": settings.solana_network,
                "simulated": False,
            }
        except Exception as e:
            logger.error("x402 escrow lock failed: %s", e)
            raise

    # Simulated fallback (no platform wallet configured or no payment header)
    bounty.escrow_tx_id = f"sim_lock_{bounty.id}"
    return {"status": "locked", "bounty_id": bounty.id, "simulated": True}


async def release_escrow(session: AsyncSession, bounty: Bounty) -> dict:
    """Release escrow to claimer on acceptance. 95% to B, 5% to platform.

    In the x402 model, funds are already on the platform wallet.
    Release = record the payout intent. Actual payout requires a separate
    Solana transaction from the platform wallet (handled by settlement cron
    or manual process).
    """
    fee = float(bounty.amount) * bounty.platform_fee_rate
    payout = float(bounty.amount) - fee

    logger.info(
        "Releasing escrow for bounty %s: %s %s → %s (claimer), %s (platform fee)",
        bounty.id, payout, bounty.currency, bounty.claimer_address, fee,
    )

    if _is_x402_configured() and bounty.escrow_tx_id and not bounty.escrow_tx_id.startswith("sim_"):
        # Real payment was made — record payout intent
        return {
            "status": "release_pending",
            "bounty_id": bounty.id,
            "escrow_tx_id": bounty.escrow_tx_id,
            "payout_amount": payout,
            "payout_currency": bounty.currency,
            "payout_to": bounty.claimer_address,
            "platform_fee": fee,
            "network": settings.solana_network,
            "note": "Payout queued. Platform wallet will send USDC to claimer.",
            "simulated": False,
        }

    return {
        "status": "released",
        "bounty_id": bounty.id,
        "payout": payout,
        "fee": fee,
        "recipient": bounty.claimer_address,
        "simulated": True,
    }


async def refund_escrow(session: AsyncSession, bounty: Bounty, partial: bool = False) -> dict:
    """Refund escrow to poster.

    partial=False: full refund (no one ever delivered)
    partial=True:  95% refund (platform keeps 5% since someone worked on it)
    """
    fee = float(bounty.amount) * bounty.platform_fee_rate if partial else 0.0
    refund_amount = float(bounty.amount) - fee

    logger.info(
        "Refunding escrow for bounty %s: %s %s → %s (poster)%s",
        bounty.id, refund_amount, bounty.currency, bounty.poster_address,
        f", {fee} platform fee kept" if partial else "",
    )

    if _is_x402_configured() and bounty.escrow_tx_id and not bounty.escrow_tx_id.startswith("sim_"):
        return {
            "status": "refund_pending",
            "bounty_id": bounty.id,
            "escrow_tx_id": bounty.escrow_tx_id,
            "refund_amount": refund_amount,
            "refund_currency": bounty.currency,
            "refund_to": bounty.poster_address,
            "platform_fee_kept": fee,
            "network": settings.solana_network,
            "note": "Refund queued. Platform wallet will send USDC back to poster.",
            "simulated": False,
        }

    return {
        "status": "refunded",
        "bounty_id": bounty.id,
        "refund": refund_amount,
        "fee_kept": fee,
        "recipient": bounty.poster_address,
        "simulated": True,
    }


# ---------------------------------------------------------------------------
# Webhook handling
# ---------------------------------------------------------------------------

def verify_webhook_signature(payload: bytes, signature: str) -> bool:
    """Verify x402 webhook signature."""
    if not settings.x402_webhook_secret:
        return False
    expected = hmac.new(
        settings.x402_webhook_secret.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def handle_webhook(session: AsyncSession, event_type: str, data: dict) -> dict:
    """Handle x402/CDP webhook events."""
    bounty_id = data.get("metadata", {}).get("bounty_id")
    if not bounty_id:
        return {"status": "ignored", "reason": "no bounty_id"}

    bounty = await session.get(Bounty, bounty_id)
    if not bounty:
        return {"status": "ignored", "reason": "bounty not found"}

    if event_type == "payment.succeeded":
        if bounty.escrow_tx_id:
            return {"status": "already_processed"}
        bounty.escrow_tx_id = data.get("tx_id") or data.get("txHash")
        await session.commit()
        return {"status": "processed", "bounty_status": bounty.status}

    elif event_type == "payment.failed":
        return {"status": "processed", "note": "payment failed, bounty stays as-is"}

    return {"status": "ignored", "reason": f"unknown event: {event_type}"}
