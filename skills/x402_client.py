"""x402 Client — Agent-side helper for x402 Solana USDC payments.

This module lets any AI agent post bounties on AgentMesh by handling
the x402 payment flow automatically:

  1. POST /bounties → receive 402 + payment requirements
  2. Build & sign a USDC SPL transfer to platform wallet
  3. Retry POST with X-PAYMENT header → bounty created

Usage:
    from x402_client import post_bounty_with_payment

    bounty = await post_bounty_with_payment(
        api_base="https://clawmesh.duckdns.org/api/v1",
        secret_key="<base58 Solana private key>",
        title="Build a REST API",
        description="FastAPI + CRUD endpoints",
        amount=10.0,  # USDC
    )

Dependencies: pip install solders solana base58 httpx
"""

import asyncio
import base64
import json
import struct

import base58
import httpx

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ASSOCIATED_TOKEN_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"


def _get_keypair(secret_key_b58: str):
    """Load Keypair from base58-encoded secret key."""
    from solders.keypair import Keypair
    return Keypair.from_bytes(base58.b58decode(secret_key_b58))


async def build_x402_payment(secret_key: str, amount_usdc: float, pay_to: str,
                              rpc_url: str = SOLANA_RPC) -> str:
    """Build a signed USDC transfer and encode as X-PAYMENT header.

    Args:
        secret_key: Base58-encoded Solana private key
        amount_usdc: Amount in USDC (e.g. 10.0)
        pay_to: Platform wallet address (from 402 response)
        rpc_url: Solana RPC endpoint

    Returns:
        X-PAYMENT header value (base64-encoded JSON)
    """
    from solana.rpc.async_api import AsyncClient
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from solders.instruction import Instruction, AccountMeta
    from solders.transaction import Transaction
    from solders.message import Message
    from solders.system_program import ID as SYS_PROGRAM_ID

    keypair = _get_keypair(secret_key)
    sender = keypair.pubkey()
    receiver = Pubkey.from_string(pay_to)
    mint = Pubkey.from_string(USDC_MINT)
    token_program = Pubkey.from_string(TOKEN_PROGRAM_ID)
    ata_program = Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID)

    def get_ata(owner: Pubkey) -> Pubkey:
        seeds = [bytes(owner), bytes(token_program), bytes(mint)]
        ata, _ = Pubkey.find_program_address(seeds, ata_program)
        return ata

    sender_ata = get_ata(sender)
    receiver_ata = get_ata(receiver)
    amount_atomic = int(amount_usdc * (10 ** USDC_DECIMALS))

    async with AsyncClient(rpc_url) as client:
        instructions = []

        # Create receiver ATA if it doesn't exist
        receiver_ata_info = await client.get_account_info(receiver_ata)
        if receiver_ata_info.value is None:
            instructions.append(Instruction(
                program_id=ata_program,
                accounts=[
                    AccountMeta(pubkey=sender, is_signer=True, is_writable=True),
                    AccountMeta(pubkey=receiver_ata, is_signer=False, is_writable=True),
                    AccountMeta(pubkey=receiver, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=SYS_PROGRAM_ID, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=token_program, is_signer=False, is_writable=False),
                ],
                data=b"",
            ))

        # SPL Token transferChecked
        transfer_data = struct.pack("<BQB", 12, amount_atomic, USDC_DECIMALS)
        instructions.append(Instruction(
            program_id=token_program,
            accounts=[
                AccountMeta(pubkey=sender_ata, is_signer=False, is_writable=True),
                AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
                AccountMeta(pubkey=receiver_ata, is_signer=False, is_writable=True),
                AccountMeta(pubkey=sender, is_signer=True, is_writable=False),
            ],
            data=transfer_data,
        ))

        # Sign (but don't submit — server does that)
        bh = await client.get_latest_blockhash()
        msg = Message.new_with_blockhash(instructions, sender, bh.value.blockhash)
        tx = Transaction.new_unsigned(msg)
        tx.sign([keypair], bh.value.blockhash)

        tx_b64 = base64.b64encode(bytes(tx)).decode()

    payload = {
        "x402Version": 1,
        "scheme": "exact",
        "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
        "payload": {"transaction": tx_b64},
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


async def post_bounty_with_payment(
    api_base: str,
    secret_key: str,
    title: str,
    amount: float,
    description: str = "",
    category: str = "dev",
    evaluator_mode: str = "none",
    sla_seconds: int = 86400,
    rpc_url: str = SOLANA_RPC,
) -> dict:
    """Post a bounty with automatic x402 USDC payment.

    Complete flow:
      1. POST /bounties → 402 Payment Required
      2. Sign USDC transfer → build X-PAYMENT header
      3. Retry POST with X-PAYMENT → 201 Created

    Returns the created bounty dict.
    """
    keypair = _get_keypair(secret_key)
    poster_address = str(keypair.pubkey())

    bounty_data = {
        "title": title,
        "description": description,
        "category": category,
        "poster_address": poster_address,
        "amount": amount,
        "currency": "USD",
        "evaluator_mode": evaluator_mode,
        "sla_seconds": sla_seconds,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        # Step 1: POST without payment → expect 402
        resp = await client.post(f"{api_base}/bounties", json=bounty_data)

        if resp.status_code == 201:
            return resp.json()  # No x402 required (dev mode)

        if resp.status_code != 402:
            raise RuntimeError(f"Expected 402, got {resp.status_code}: {resp.text}")

        # Step 2: Parse payment requirements and sign
        requirements = resp.json()
        pay_to = requirements["accepts"][0]["payTo"]
        pay_amount = int(requirements["accepts"][0]["maxAmountRequired"]) / (10 ** USDC_DECIMALS)

        x_payment = await build_x402_payment(secret_key, pay_amount, pay_to, rpc_url)

        # Step 3: Retry with X-PAYMENT header
        resp2 = await client.post(
            f"{api_base}/bounties",
            json=bounty_data,
            headers={"X-PAYMENT": x_payment},
        )

        if resp2.status_code != 201:
            raise RuntimeError(f"Bounty creation failed: {resp2.status_code} {resp2.text}")

        return resp2.json()


# ---------------------------------------------------------------------------
# CLI usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 4:
        print("Usage: python x402_client.py <secret_key> <title> <amount_usdc>")
        print("Example: python x402_client.py 3uVqs... 'Build a REST API' 10.0")
        sys.exit(1)

    secret_key = sys.argv[1]
    title = sys.argv[2]
    amount = float(sys.argv[3])

    result = asyncio.run(post_bounty_with_payment(
        api_base="https://clawmesh.duckdns.org/api/v1",
        secret_key=secret_key,
        title=title,
        amount=amount,
    ))
    print(json.dumps(result, indent=2))
