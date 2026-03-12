"""ERC-8183 Evaluator — AI-powered third-party delivery evaluation.

Three evaluator modes:
  none        — Poster reviews manually (5% fee)
  platform_ai — Platform's AI evaluates delivery quality (8% fee)
  custom      — External evaluator agent at evaluator_address (8% fee)

Platform AI evaluator uses Claude to:
  1. Compare the delivery output against the task requirements
  2. Score quality, completeness, and adherence to spec
  3. Return accept / request_revision / reject with reasoning
"""

import json
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.bounty import Bounty

logger = logging.getLogger(__name__)

# Fee rates by evaluator mode
FEE_RATES = {
    "none": 0.05,        # 5% — poster self-reviews
    "platform_ai": 0.15, # 15% — platform AI evaluates (includes evaluation cost)
    "custom": 0.15,      # 15% — custom evaluator
}

EVALUATOR_PROMPT = """You are an impartial third-party evaluator for AgentMesh, an agent-to-agent task marketplace.
Your role is to objectively assess whether a delivery meets the task requirements.
You must be fair to both the poster (client) and the claimer (provider).

## Task Requirements
Title: {title}
Description: {description}
Category: {category}
Input/Requirements: {input_payload}
Expected Output Schema: {output_schema}

## Delivery Output
{output_payload}

## Evaluation Criteria
1. **Completeness** (0-10): Does the delivery address all requirements?
2. **Quality** (0-10): Is the work of acceptable quality?
3. **Adherence** (0-10): Does it match the expected format/schema?
4. **Overall** (0-10): Would a reasonable client accept this?

## Instructions
- Score each criterion 0-10
- If overall >= 7: recommend ACCEPT
- If overall 4-6: recommend REVISION with specific feedback
- If overall < 4: recommend REJECT with reasoning
- Be objective. Do not favor either party.

Respond in this exact JSON format:
{{
  "completeness": <0-10>,
  "quality": <0-10>,
  "adherence": <0-10>,
  "overall": <0-10>,
  "decision": "accept" | "revision" | "reject",
  "reasoning": "<2-3 sentence explanation>",
  "revision_feedback": "<specific feedback if decision is revision, else null>"
}}
"""


def get_fee_rate(evaluator_mode: str) -> float:
    """Get platform fee rate based on evaluator mode."""
    return FEE_RATES.get(evaluator_mode, 0.05)


async def evaluate_delivery(session: AsyncSession, bounty: Bounty) -> dict:
    """Evaluate a bounty delivery using the configured evaluator.

    Returns evaluation result dict with decision and scores.
    """
    if bounty.evaluator_mode == "platform_ai":
        return await _evaluate_with_platform_ai(bounty)
    elif bounty.evaluator_mode == "custom":
        return await _evaluate_with_custom(bounty)
    else:
        # No evaluator — shouldn't be called, but return passthrough
        return {"decision": "accept", "reasoning": "No evaluator configured", "overall": 10}


async def _evaluate_with_platform_ai(bounty: Bounty) -> dict:
    """Use DeepSeek to evaluate delivery quality."""
    if not settings.deepseek_api_key:
        logger.warning("No deepseek_api_key configured, auto-accepting delivery")
        return {
            "decision": "accept",
            "reasoning": "AI evaluator not configured (no API key). Auto-accepted.",
            "overall": 10,
            "simulated": True,
        }

    prompt = EVALUATOR_PROMPT.format(
        title=bounty.title or "",
        description=bounty.description or "No description provided",
        category=bounty.category or "general",
        input_payload=json.dumps(bounty.input_payload, indent=2) if bounty.input_payload else "None specified",
        output_schema=json.dumps(bounty.output_schema, indent=2) if bounty.output_schema else "None specified",
        output_payload=json.dumps(bounty.output_payload, indent=2) if bounty.output_payload else "Empty delivery",
    )

    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
        )
        response = client.chat.completions.create(
            model=settings.deepseek_model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )

        text = response.choices[0].message.content
        # Extract JSON from response (handle markdown code blocks)
        if "```" in text:
            text = text.split("```json")[-1].split("```")[0] if "```json" in text else text.split("```")[1].split("```")[0]

        result = json.loads(text.strip())
        result["evaluator"] = "platform_ai"
        result["model"] = settings.deepseek_model

        logger.info(
            "Platform AI evaluation for bounty %s: decision=%s, overall=%s",
            bounty.id, result.get("decision"), result.get("overall"),
        )
        return result

    except Exception as e:
        logger.error("Platform AI evaluation failed for bounty %s: %s", bounty.id, e)
        # On failure, don't block the flow — return a neutral result
        return {
            "decision": "accept",
            "reasoning": f"AI evaluation encountered an error ({e}). Auto-accepted to prevent blocking.",
            "overall": 7,
            "error": str(e),
            "evaluator": "platform_ai_fallback",
        }


async def _evaluate_with_custom(bounty: Bounty) -> dict:
    """Call external evaluator agent at evaluator_address."""
    if not bounty.evaluator_address:
        return {
            "decision": "accept",
            "reasoning": "No evaluator address configured. Auto-accepted.",
            "overall": 10,
        }

    payload = {
        "bounty_id": bounty.id,
        "title": bounty.title,
        "description": bounty.description,
        "category": bounty.category,
        "input_payload": bounty.input_payload,
        "output_schema": bounty.output_schema,
        "output_payload": bounty.output_payload,
        "poster_address": bounty.poster_address,
        "claimer_address": bounty.claimer_address,
        "amount": float(bounty.amount),
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                bounty.evaluator_address,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            result = resp.json()

            # Validate required fields
            if "decision" not in result:
                raise ValueError("Evaluator response missing 'decision' field")
            if result["decision"] not in ("accept", "revision", "reject"):
                raise ValueError(f"Invalid decision: {result['decision']}")

            result["evaluator"] = "custom"
            result["evaluator_address"] = bounty.evaluator_address
            return result

    except Exception as e:
        logger.error("Custom evaluator failed for bounty %s: %s", bounty.id, e)
        return {
            "decision": "accept",
            "reasoning": f"Custom evaluator failed ({e}). Auto-accepted to prevent blocking.",
            "overall": 7,
            "error": str(e),
            "evaluator": "custom_fallback",
        }
