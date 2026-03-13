"""x402 Solana payment service — real USDC escrow via CDP facilitator.

x402 flow (收钱 — Agent A → 平台):
  1. Client POSTs to create bounty (no X-PAYMENT header)
  2. Server returns 402 with payment requirements (USDC amount, payTo wallet)
  3. Client signs Solana SPL transfer, retries with X-PAYMENT header
  4. Server verifies via CDP facilitator
  5. Server creates bounty, then settles payment via CDP

Payout flow (打钱 — 平台 → Agent B / 退款 → Agent A):
  Platform wallet derives keypair from mnemonic, builds SPL Token transfer,
  signs and sends on-chain. Fully automatic.

Money flow:
  A posts bounty (100 USDC) → 100 locked (paid to platform wallet)
  B delivers → A accepts → B gets 85-95, platform keeps 5-15
  A cancels (never delivered) → A gets 100 back
  A cancels (after delivery) → A gets 85-95 back, platform keeps 5-15
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

# Solana RPC endpoints
SOLANA_RPC_URLS = {
    "devnet": "https://api.devnet.solana.com",
    "mainnet": "https://api.mainnet-beta.solana.com",
}

# SPL Token Program ID
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ASSOCIATED_TOKEN_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
SYSVAR_RENT_ID = "SysvarRent111111111111111111111111111111111"


def _is_x402_configured() -> bool:
    """Check if x402 payment collection is configured.

    Supports two modes:
      1. CDP facilitator (x402_cdp_api_key + secret set)
      2. Self-facilitated (platform_wallet_address set, we verify & submit tx ourselves)
    """
    return bool(settings.platform_wallet_address)


def _is_payout_configured() -> bool:
    """Check if automatic on-chain payout is configured."""
    return bool(settings.platform_wallet_mnemonic and settings.platform_wallet_address)


def _usd_to_usdc_atomic(amount: float) -> str:
    """Convert USD amount to USDC smallest units (6 decimals). 1 USDC ≈ 1 USD."""
    return str(int(amount * (10 ** USDC_DECIMALS)))


def _get_rpc_url() -> str:
    """Get Solana RPC URL."""
    if settings.solana_rpc_url:
        return settings.solana_rpc_url
    return SOLANA_RPC_URLS.get(settings.solana_network, SOLANA_RPC_URLS["devnet"])


def _create_cdp_jwt(uri: str) -> str:
    """Create a JWT for CDP API authentication (Ed25519/EdDSA)."""
    import jwt  # PyJWT

    key_bytes = base64.b64decode(settings.x402_cdp_api_secret)
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
# Wallet key derivation (from mnemonic)
# ---------------------------------------------------------------------------

def _derive_keypair():
    """Derive Solana Keypair from platform mnemonic (BIP44 m/44'/501'/0'/0')."""
    from mnemonic import Mnemonic
    from solders.keypair import Keypair

    m = Mnemonic("english")
    seed = m.to_seed(settings.platform_wallet_mnemonic, "")

    # SLIP-0010 Ed25519 derivation
    def _derive_ed25519(seed_bytes: bytes, path: list[int]) -> bytes:
        I = hmac.new(b"ed25519 seed", seed_bytes, hashlib.sha512).digest()
        key, chain_code = I[:32], I[32:]
        for idx in path:
            idx = idx | 0x80000000  # hardened
            data = b"\x00" + key + idx.to_bytes(4, "big")
            I = hmac.new(chain_code, data, hashlib.sha512).digest()
            key, chain_code = I[:32], I[32:]
        return key

    private_key_bytes = _derive_ed25519(seed, [44, 501, 0, 0])
    kp = Keypair.from_seed(private_key_bytes)

    # Safety: verify derived address matches configured address
    if str(kp.pubkey()) != settings.platform_wallet_address:
        raise RuntimeError(
            f"Derived wallet {kp.pubkey()} does not match configured "
            f"platform_wallet_address {settings.platform_wallet_address}"
        )
    return kp


# ---------------------------------------------------------------------------
# On-chain USDC transfer (platform wallet → recipient)
# ---------------------------------------------------------------------------

async def _send_usdc(recipient_address: str, amount_usdc: float) -> str:
    """Send USDC from platform wallet to recipient. Returns tx signature.

    Steps:
      1. Derive platform keypair from mnemonic
      2. Find/create Associated Token Accounts for sender and receiver
      3. Build SPL Token transferChecked instruction
      4. Sign and send transaction
    """
    from solana.rpc.async_api import AsyncClient
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from solders.instruction import Instruction, AccountMeta
    from solders.transaction import Transaction
    from solders.message import Message
    from solders.system_program import ID as SYS_PROGRAM_ID

    # Validate recipient address
    try:
        receiver = Pubkey.from_string(recipient_address)
    except Exception:
        raise ValueError(f"Invalid Solana address: {recipient_address}")

    keypair = _derive_keypair()
    rpc_url = _get_rpc_url()
    mint = Pubkey.from_string(settings.usdc_mint_address)
    sender = keypair.pubkey()
    token_program = Pubkey.from_string(TOKEN_PROGRAM_ID)
    ata_program = Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID)

    # Compute Associated Token Addresses
    def _get_ata(owner: Pubkey) -> Pubkey:
        seeds = [bytes(owner), bytes(token_program), bytes(mint)]
        ata, _ = Pubkey.find_program_address(seeds, ata_program)
        return ata

    sender_ata = _get_ata(sender)
    receiver_ata = _get_ata(receiver)

    amount_atomic = int(amount_usdc * (10 ** USDC_DECIMALS))

    async with AsyncClient(rpc_url) as client:
        # Check platform USDC balance before attempting transfer
        sender_ata_info = await client.get_token_account_balance(sender_ata)
        if sender_ata_info.value:
            balance_atomic = int(sender_ata_info.value.amount)
            if balance_atomic < amount_atomic:
                balance_usdc = balance_atomic / (10 ** USDC_DECIMALS)
                raise ValueError(
                    f"Insufficient USDC balance: have {balance_usdc}, need {amount_usdc}"
                )
        else:
            raise ValueError("Platform wallet has no USDC token account")

        # Check if receiver ATA exists
        receiver_ata_info = await client.get_account_info(receiver_ata)
        instructions = []

        if receiver_ata_info.value is None:
            # Create ATA for receiver (platform pays rent)
            create_ata_ix = Instruction(
                program_id=ata_program,
                accounts=[
                    AccountMeta(pubkey=sender, is_signer=True, is_writable=True),       # payer
                    AccountMeta(pubkey=receiver_ata, is_signer=False, is_writable=True), # ata
                    AccountMeta(pubkey=receiver, is_signer=False, is_writable=False),    # owner
                    AccountMeta(pubkey=mint, is_signer=False, is_writable=False),        # mint
                    AccountMeta(pubkey=SYS_PROGRAM_ID, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=token_program, is_signer=False, is_writable=False),
                ],
                data=b"",  # CreateAssociatedTokenAccount has no data
            )
            instructions.append(create_ata_ix)
            logger.info("Will create ATA for receiver %s", recipient_address)

        # SPL Token transferChecked instruction
        # Instruction data: [12 (u8 discriminator)] + [amount (u64 LE)] + [decimals (u8)]
        import struct
        transfer_data = struct.pack("<BQB", 12, amount_atomic, USDC_DECIMALS)

        transfer_ix = Instruction(
            program_id=token_program,
            accounts=[
                AccountMeta(pubkey=sender_ata, is_signer=False, is_writable=True),   # source
                AccountMeta(pubkey=mint, is_signer=False, is_writable=False),         # mint
                AccountMeta(pubkey=receiver_ata, is_signer=False, is_writable=True),  # destination
                AccountMeta(pubkey=sender, is_signer=True, is_writable=False),        # authority
            ],
            data=transfer_data,
        )
        instructions.append(transfer_ix)

        # Get recent blockhash
        blockhash_resp = await client.get_latest_blockhash()
        recent_blockhash = blockhash_resp.value.blockhash

        # Build and sign transaction
        msg = Message.new_with_blockhash(instructions, sender, recent_blockhash)
        tx = Transaction.new_unsigned(msg)
        tx.sign([keypair], recent_blockhash)

        # Send
        send_resp = await client.send_transaction(tx)
        signature = str(send_resp.value)

        logger.info(
            "USDC transfer sent: %s USDC → %s, tx=%s",
            amount_usdc, recipient_address, signature,
        )

        # Confirm transaction — poll until confirmed or timeout
        import asyncio as _asyncio
        for attempt in range(30):
            await _asyncio.sleep(1)
            try:
                status_resp = await client.get_signature_statuses([send_resp.value])
                statuses = status_resp.value
                if statuses and statuses[0] is not None:
                    if statuses[0].err is None:
                        logger.info("Transaction confirmed: %s", signature)
                        break
                    else:
                        raise ValueError(f"Transaction failed on-chain: {statuses[0].err}")
            except ValueError:
                raise
            except Exception as e:
                logger.warning("Confirm poll attempt %d: %s", attempt, e)
        else:
            logger.warning("Confirmation timed out for tx=%s, proceeding", signature)

        return signature


# ---------------------------------------------------------------------------
# Payment Requirements (402 response)
# ---------------------------------------------------------------------------

def build_payment_requirements(amount: float, resource: str, description: str = "") -> dict:
    """Build x402 payment requirements for a 402 response."""
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
# x402 Verification & Settlement
# ---------------------------------------------------------------------------

def _has_cdp_credentials() -> bool:
    """Check if CDP facilitator credentials are configured."""
    return bool(settings.x402_cdp_api_key and settings.x402_cdp_api_secret)


async def verify_and_settle_x402(x_payment_header: str, payment_requirements: dict) -> dict:
    """Verify and settle an x402 payment.

    Two modes:
      1. CDP facilitator (if credentials configured) — delegates to Coinbase
      2. Self-facilitated — deserialize the signed tx, validate it, submit on-chain

    Returns: {"txHash": "<signature>"}
    """
    try:
        payment = json.loads(base64.b64decode(x_payment_header))
    except Exception as e:
        raise ValueError(f"Invalid X-PAYMENT header encoding: {e}")

    if _has_cdp_credentials():
        return await _verify_settle_via_cdp(payment, payment_requirements)
    else:
        return await _verify_settle_self(payment, payment_requirements)


async def _verify_settle_via_cdp(payment: dict, payment_requirements: dict) -> dict:
    """Verify + settle via CDP facilitator (original x402 flow)."""
    verify_url = f"{settings.x402_facilitator_url}/verify"
    token = _create_cdp_jwt(verify_url)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            verify_url,
            json={"x402Version": 1, "payment": payment, "paymentRequirements": payment_requirements},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            logger.error("CDP verify failed [%d]: %s", resp.status_code, resp.text)
            raise ValueError(f"Payment verification failed: {resp.text}")
        result = resp.json()
        if not result.get("valid", False):
            raise ValueError("Payment verification returned invalid")

    settle_url = f"{settings.x402_facilitator_url}/settle"
    token = _create_cdp_jwt(settle_url)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            settle_url,
            json={"x402Version": 1, "payment": payment, "paymentRequirements": payment_requirements},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            logger.error("CDP settle failed [%d]: %s", resp.status_code, resp.text)
            raise ValueError(f"Payment settlement failed: {resp.text}")
        return resp.json()


async def _verify_settle_self(payment: dict, payment_requirements: dict) -> dict:
    """Self-facilitated x402: verify the signed transaction and submit it on-chain.

    Validation:
      1. Deserialize the signed Solana transaction
      2. Check it contains a SPL Token transfer to platform wallet's ATA
      3. Check the amount matches payment requirements
      4. Submit the transaction on-chain
      5. Confirm it
    """
    from solana.rpc.async_api import AsyncClient
    from solders.transaction import Transaction
    from solders.pubkey import Pubkey

    # Extract the signed transaction from the payment payload
    tx_b64 = payment.get("payload", {}).get("transaction")
    if not tx_b64:
        raise ValueError("Missing payload.transaction in x402 payment")

    # Deserialize the transaction
    try:
        tx_bytes = base64.b64decode(tx_b64)
        tx = Transaction.from_bytes(tx_bytes)
    except Exception as e:
        raise ValueError(f"Cannot deserialize transaction: {e}")

    logger.info("Self-verifying x402 payment: %d bytes, %d sigs", len(tx_bytes), len(tx.signatures))

    # Validate: check the transaction transfers USDC to platform wallet
    platform_wallet = Pubkey.from_string(settings.platform_wallet_address)
    mint = Pubkey.from_string(settings.usdc_mint_address)
    token_program = Pubkey.from_string(TOKEN_PROGRAM_ID)
    ata_program = Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID)

    # Compute platform wallet's USDC ATA
    platform_ata, _ = Pubkey.find_program_address(
        [bytes(platform_wallet), bytes(token_program), bytes(mint)],
        ata_program,
    )

    # Check the transaction contains a transfer to platform ATA
    expected_amount = int(payment_requirements["accepts"][0]["maxAmountRequired"])
    msg = tx.message
    account_keys = msg.account_keys

    found_transfer = False
    for ix in msg.instructions:
        program_id = account_keys[ix.program_id_index]
        if str(program_id) != TOKEN_PROGRAM_ID:
            continue

        ix_data = bytes(ix.data)
        if len(ix_data) < 10 or ix_data[0] != 12:  # transferChecked discriminator
            continue

        # Parse: [12 (u8)] [amount (u64 LE)] [decimals (u8)]
        import struct
        _, amount, decimals = struct.unpack("<BQB", ix_data[:10])

        # Check destination is platform ATA
        dest_idx = ix.accounts[2]  # transferChecked: [source, mint, dest, authority]
        dest_key = account_keys[dest_idx]

        if str(dest_key) == str(platform_ata) and amount >= expected_amount:
            found_transfer = True
            logger.info(
                "Verified transfer: %d USDC atomic → platform ATA %s",
                amount, platform_ata,
            )
            break

    if not found_transfer:
        raise ValueError(
            f"Transaction does not contain a valid USDC transfer of "
            f"{expected_amount} atomic to platform ATA {platform_ata}"
        )

    # Transaction is valid — submit it on-chain
    rpc_url = _get_rpc_url()
    async with AsyncClient(rpc_url) as client:
        send_resp = await client.send_transaction(tx)
        signature = str(send_resp.value)
        logger.info("x402 payment submitted on-chain: tx=%s", signature)

        # Confirm — poll getSignatureStatuses until finalized or timeout
        import asyncio as _asyncio
        for attempt in range(30):
            await _asyncio.sleep(1)
            try:
                status_resp = await client.get_signature_statuses([send_resp.value])
                statuses = status_resp.value
                if statuses and statuses[0] is not None:
                    if statuses[0].err is None:
                        logger.info("x402 payment confirmed: tx=%s", signature)
                        break
                    else:
                        raise ValueError(f"Transaction failed on-chain: {statuses[0].err}")
            except ValueError:
                raise
            except Exception as e:
                logger.warning("Confirm poll attempt %d failed: %s", attempt, e)
        else:
            logger.warning("Confirmation timed out for tx=%s, proceeding (tx was sent)", signature)

    return {"txHash": signature}


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
        try:
            result = await verify_and_settle_x402(x_payment_header, payment_requirements)

            tx_id = result.get("txHash") or result.get("transaction_hash", "")
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
    """Release escrow to claimer on acceptance.

    Calculates payout based on platform_fee_rate, then sends USDC on-chain.
    """
    fee = float(bounty.amount) * bounty.platform_fee_rate
    payout = float(bounty.amount) - fee

    logger.info(
        "Releasing escrow for bounty %s: %s USDC → %s (claimer), %s (platform fee)",
        bounty.id, payout, bounty.claimer_address, fee,
    )

    is_real_payment = (
        bounty.escrow_tx_id
        and not bounty.escrow_tx_id.startswith("sim_")
    )

    if is_real_payment and _is_payout_configured():
        # Real on-chain payout
        try:
            tx_sig = await _send_usdc(bounty.claimer_address, payout)
            bounty.payout_tx_id = tx_sig
            await session.commit()

            logger.info("Payout completed: %s USDC → %s, tx=%s", payout, bounty.claimer_address, tx_sig)
            return {
                "status": "released",
                "bounty_id": bounty.id,
                "payout_amount": payout,
                "payout_to": bounty.claimer_address,
                "platform_fee": fee,
                "tx_id": tx_sig,
                "network": settings.solana_network,
                "simulated": False,
            }
        except Exception as e:
            logger.error("On-chain payout failed for bounty %s: %s", bounty.id, e)
            # Record failure but don't block acceptance — can retry later
            return {
                "status": "release_failed",
                "bounty_id": bounty.id,
                "payout_amount": payout,
                "payout_to": bounty.claimer_address,
                "error": str(e),
                "note": "Payout failed. Will retry or require manual intervention.",
                "simulated": False,
            }

    # Simulated
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
    partial=True:  refund minus platform fee (someone worked on it)
    """
    fee = float(bounty.amount) * bounty.platform_fee_rate if partial else 0.0
    refund_amount = float(bounty.amount) - fee

    logger.info(
        "Refunding escrow for bounty %s: %s USDC → %s (poster)%s",
        bounty.id, refund_amount, bounty.poster_address,
        f", {fee} platform fee kept" if partial else "",
    )

    is_real_payment = (
        bounty.escrow_tx_id
        and not bounty.escrow_tx_id.startswith("sim_")
    )

    if is_real_payment and _is_payout_configured():
        # Real on-chain refund
        try:
            tx_sig = await _send_usdc(bounty.poster_address, refund_amount)
            bounty.refund_tx_id = tx_sig
            await session.commit()

            logger.info("Refund completed: %s USDC → %s, tx=%s", refund_amount, bounty.poster_address, tx_sig)
            return {
                "status": "refunded",
                "bounty_id": bounty.id,
                "refund_amount": refund_amount,
                "refund_to": bounty.poster_address,
                "platform_fee_kept": fee,
                "tx_id": tx_sig,
                "network": settings.solana_network,
                "simulated": False,
            }
        except Exception as e:
            logger.error("On-chain refund failed for bounty %s: %s", bounty.id, e)
            return {
                "status": "refund_failed",
                "bounty_id": bounty.id,
                "refund_amount": refund_amount,
                "refund_to": bounty.poster_address,
                "error": str(e),
                "note": "Refund failed. Will retry or require manual intervention.",
                "simulated": False,
            }

    # Simulated
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
