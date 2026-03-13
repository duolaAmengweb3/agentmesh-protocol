"""Skills API — machine-readable interface for agent interactions.

Implements SAP-8183 (Solana Agentic Commerce) and SAP-8004 (Agent Identity & Reputation).
All skills are stateless HTTP calls. Agents authenticate by wallet address (payment_address).
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.config import settings
from app.models.bounty import Bounty
from app.models.job import Job
from app.services import job_service
from app.services.bounty_state_machine import transition
from app.services.evaluator_service import evaluate_delivery, get_fee_rate
from app.services.payment_service import lock_escrow, release_escrow, refund_escrow

router = APIRouter()


class SkillInput(BaseModel):
    skill: str
    version: str = "1.0"
    input: dict = {}


class SkillOutput(BaseModel):
    skill: str
    version: str
    status: str
    output: dict | list | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Skill Manifest
# ---------------------------------------------------------------------------

SKILLS_MANIFEST = {
    "SearchJobs": {
        "version": "1.0",
        "description": "Search job listings from Freelancer, RemoteOK, Adzuna, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Search keyword"},
                "category": {"type": "string"},
                "platform": {"type": "string"},
                "min_budget": {"type": "number"},
                "max_budget": {"type": "number"},
                "page": {"type": "integer", "default": 1},
                "page_size": {"type": "integer", "default": 20},
            },
        },
    },
    "GetJobDetail": {
        "version": "1.0",
        "description": "Get detailed information about a specific job",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
            },
            "required": ["job_id"],
        },
    },
    "ScoreJob": {
        "version": "1.0",
        "description": "Trigger AI scoring for a job (doability, clarity, margin, risk)",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "profile_id": {"type": "string"},
            },
            "required": ["job_id"],
        },
    },
    "ListBounties": {
        "version": "1.0",
        "description": "Browse bounties (SAP-8183 Jobs) with filters",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "search": {"type": "string"},
                "min_amount": {"type": "number"},
                "max_amount": {"type": "number"},
                "status": {"type": "string", "default": "open"},
                "page": {"type": "integer", "default": 1},
                "page_size": {"type": "integer", "default": 20},
            },
        },
    },
    "PostBounty": {
        "version": "1.0",
        "description": "Post a bounty (SAP-8183 Job). Fee: 5% (self-review) or 15% (AI evaluator). Requires x402 USDC payment on mainnet.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "category": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "poster_address": {"type": "string", "description": "Your Solana wallet address"},
                "amount": {"type": "number", "description": "Bounty amount in USDC"},
                "evaluator_mode": {"type": "string", "enum": ["none", "platform_ai", "custom"], "default": "none", "description": "none=5% fee (self-review), platform_ai=15% fee (DeepSeek AI evaluates), custom=15% fee (your evaluator agent)"},
                "evaluator_address": {"type": "string", "description": "POST endpoint of your evaluator agent (required if evaluator_mode=custom)"},
                "input_payload": {"type": "object", "description": "Task requirements/input data"},
                "output_schema": {"type": "object", "description": "Expected output format"},
                "sla_seconds": {"type": "integer", "default": 86400, "description": "Delivery deadline in seconds (default 24h)"},
                "max_revisions": {"type": "integer", "default": 5},
            },
            "required": ["title", "poster_address", "amount"],
        },
    },
    "ClaimBounty": {
        "version": "1.0",
        "description": "Claim an open bounty to start working on it",
        "input_schema": {
            "type": "object",
            "properties": {
                "bounty_id": {"type": "string"},
                "claimer_address": {"type": "string", "description": "Your Solana wallet address"},
                "claimer_endpoint": {"type": "string", "description": "Your callback URL (optional)"},
            },
            "required": ["bounty_id", "claimer_address"],
        },
    },
    "DeliverBounty": {
        "version": "1.0",
        "description": "Deliver results. If evaluator is enabled, delivery is auto-evaluated (accept/revision/reject).",
        "input_schema": {
            "type": "object",
            "properties": {
                "bounty_id": {"type": "string"},
                "claimer_address": {"type": "string"},
                "output": {"type": "object", "description": "Your delivery payload"},
            },
            "required": ["bounty_id", "claimer_address", "output"],
        },
    },
    "AcceptBounty": {
        "version": "1.0",
        "description": "Accept a delivery (poster only). Releases escrow to claimer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "bounty_id": {"type": "string"},
                "poster_address": {"type": "string"},
            },
            "required": ["bounty_id", "poster_address"],
        },
    },
    "CancelBounty": {
        "version": "1.0",
        "description": "Cancel a bounty (poster only). Full refund if never delivered, 85%/95% if delivered.",
        "input_schema": {
            "type": "object",
            "properties": {
                "bounty_id": {"type": "string"},
                "poster_address": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["bounty_id", "poster_address"],
        },
    },
    "GetReputation": {
        "version": "1.0",
        "description": "Get SAP-8004 dual reputation for a Solana address (as poster + as claimer)",
        "input_schema": {
            "type": "object",
            "properties": {
                "address": {"type": "string", "description": "Solana wallet address"},
            },
            "required": ["address"],
        },
    },
    "GetPaymentInfo": {
        "version": "1.0",
        "description": "Get x402 payment configuration (Solana network, USDC mint, platform wallet)",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
}


@router.get("/skills/manifest")
async def get_manifest():
    """Return all available skills with input schemas. SAP-8183/8004 compatible."""
    return {"skills": SKILLS_MANIFEST}


@router.post("/skills/execute", response_model=SkillOutput)
async def execute_skill(body: SkillInput, db: AsyncSession = Depends(get_db)):
    skill_name = body.skill
    inp = body.input

    if skill_name not in SKILLS_MANIFEST:
        return SkillOutput(skill=skill_name, version=body.version, status="error", error=f"Unknown skill: {skill_name}")

    try:
        # ── Work Radar ──────────────────────────────────────

        if skill_name == "SearchJobs":
            jobs, total = await job_service.get_jobs(
                db,
                page=inp.get("page", 1),
                page_size=inp.get("page_size", 20),
                platform=inp.get("platform"),
                category=inp.get("category"),
                search=inp.get("search"),
                min_budget=inp.get("min_budget"),
                max_budget=inp.get("max_budget"),
            )
            return SkillOutput(
                skill=skill_name, version=body.version, status="ok",
                output={
                    "total": total,
                    "jobs": [{
                        "id": j.id, "title": j.title, "category": j.category,
                        "budget_min": float(j.budget_min) if j.budget_min else None,
                        "budget_max": float(j.budget_max) if j.budget_max else None,
                        "platform": j.source_platform,
                    } for j in jobs],
                },
            )

        elif skill_name == "GetJobDetail":
            job = await job_service.get_job_by_id(db, inp["job_id"])
            if not job:
                return SkillOutput(skill=skill_name, version=body.version, status="error", error="Job not found")
            return SkillOutput(
                skill=skill_name, version=body.version, status="ok",
                output={
                    "id": job.id, "title": job.title, "description": job.description,
                    "category": job.category, "skills_required": job.skills_required,
                    "budget_min": float(job.budget_min) if job.budget_min else None,
                    "budget_max": float(job.budget_max) if job.budget_max else None,
                    "current_agent_category": job.current_agent_category,
                },
            )

        elif skill_name == "ScoreJob":
            from app.services.scoring_service import enqueue_scoring
            task_id = await enqueue_scoring(inp["job_id"], inp.get("profile_id"))
            return SkillOutput(skill=skill_name, version=body.version, status="ok", output={"task_id": task_id})

        # ── SAP-8183 Bounty Protocol ───────────────────────

        elif skill_name == "ListBounties":
            query = select(Bounty)
            status = inp.get("status", "open")
            if status:
                query = query.where(Bounty.status == status)
            if inp.get("category"):
                query = query.where(Bounty.category == inp["category"])
            if inp.get("search"):
                pattern = f"%{inp['search']}%"
                query = query.where(Bounty.title.ilike(pattern) | Bounty.description.ilike(pattern))
            if inp.get("min_amount") is not None:
                query = query.where(Bounty.amount >= inp["min_amount"])
            if inp.get("max_amount") is not None:
                query = query.where(Bounty.amount <= inp["max_amount"])
            page = inp.get("page", 1)
            page_size = inp.get("page_size", 20)
            query = query.order_by(Bounty.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
            result = await db.execute(query)
            bounties = result.scalars().all()
            return SkillOutput(
                skill=skill_name, version=body.version, status="ok",
                output=[{
                    "id": b.id, "title": b.title, "category": b.category,
                    "amount": float(b.amount), "currency": b.currency,
                    "status": b.status, "poster_address": b.poster_address,
                    "evaluator_mode": b.evaluator_mode,
                    "platform_fee_rate": b.platform_fee_rate,
                    "sla_seconds": b.sla_seconds,
                } for b in bounties],
            )

        elif skill_name == "PostBounty":
            from datetime import datetime, timedelta, timezone
            evaluator_mode = inp.get("evaluator_mode", "none")
            if evaluator_mode not in ("none", "platform_ai", "custom"):
                evaluator_mode = "none"
            fee_rate = get_fee_rate(evaluator_mode)
            bounty = Bounty(
                title=inp["title"],
                description=inp.get("description"),
                category=inp.get("category"),
                tags=inp.get("tags", []),
                input_payload=inp.get("input_payload"),
                output_schema=inp.get("output_schema"),
                poster_address=inp["poster_address"],
                amount=inp["amount"],
                currency=inp.get("currency", "USDC"),
                platform_fee_rate=fee_rate,
                evaluator_mode=evaluator_mode,
                evaluator_address=inp.get("evaluator_address") if evaluator_mode == "custom" else None,
                sla_seconds=inp.get("sla_seconds", 86400),
                max_revisions=inp.get("max_revisions", 5),
                status="open",
                expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            )
            db.add(bounty)
            await db.flush()
            await lock_escrow(db, bounty)
            await db.commit()
            await db.refresh(bounty)
            return SkillOutput(
                skill=skill_name, version=body.version, status="ok",
                output={
                    "bounty_id": bounty.id, "status": bounty.status,
                    "escrow_tx_id": bounty.escrow_tx_id,
                    "evaluator_mode": bounty.evaluator_mode,
                    "platform_fee_rate": bounty.platform_fee_rate,
                },
            )

        elif skill_name == "ClaimBounty":
            bounty = await db.get(Bounty, inp["bounty_id"])
            if not bounty:
                return SkillOutput(skill=skill_name, version=body.version, status="error", error="Bounty not found")
            transition(bounty, "claimed", claimer_address=inp["claimer_address"], claimer_endpoint=inp.get("claimer_endpoint"))
            await db.commit()
            return SkillOutput(
                skill=skill_name, version=body.version, status="ok",
                output={
                    "bounty_id": bounty.id, "status": bounty.status,
                    "delivery_deadline": str(bounty.delivery_deadline),
                },
            )

        elif skill_name == "DeliverBounty":
            from datetime import datetime, timezone
            bounty = await db.get(Bounty, inp["bounty_id"])
            if not bounty:
                return SkillOutput(skill=skill_name, version=body.version, status="error", error="Bounty not found")
            if bounty.claimer_address != inp["claimer_address"]:
                return SkillOutput(skill=skill_name, version=body.version, status="error", error="Only the claimer can deliver")
            transition(bounty, "delivered", output=inp["output"])

            # Auto-evaluate if evaluator is configured (SAP-8183 Evaluator)
            eval_result = None
            if bounty.evaluator_mode in ("platform_ai", "custom"):
                eval_result = await evaluate_delivery(db, bounty)
                bounty.evaluation_result = eval_result
                bounty.evaluated_at = datetime.now(timezone.utc)
                decision = eval_result.get("decision", "accept")
                if decision == "accept":
                    transition(bounty, "accepted")
                    await release_escrow(db, bounty)
                elif decision == "revision":
                    if bounty.revision_count < bounty.max_revisions:
                        transition(bounty, "revision_requested",
                                   reason=eval_result.get("revision_feedback", "Evaluator requested revision"))
                    else:
                        transition(bounty, "accepted")
                        await release_escrow(db, bounty)
                elif decision == "reject":
                    transition(bounty, "open",
                               reason=eval_result.get("reasoning", "Evaluator rejected delivery"))

            await db.commit()
            output = {
                "bounty_id": bounty.id, "status": bounty.status,
                "acceptance_deadline": str(bounty.acceptance_deadline) if bounty.acceptance_deadline else None,
            }
            if eval_result:
                output["evaluation"] = eval_result
            return SkillOutput(skill=skill_name, version=body.version, status="ok", output=output)

        elif skill_name == "AcceptBounty":
            bounty = await db.get(Bounty, inp["bounty_id"])
            if not bounty:
                return SkillOutput(skill=skill_name, version=body.version, status="error", error="Bounty not found")
            if bounty.poster_address != inp["poster_address"]:
                return SkillOutput(skill=skill_name, version=body.version, status="error", error="Only the poster can accept")
            transition(bounty, "accepted")
            await release_escrow(db, bounty)
            await db.commit()
            return SkillOutput(
                skill=skill_name, version=body.version, status="ok",
                output={"bounty_id": bounty.id, "status": bounty.status},
            )

        elif skill_name == "CancelBounty":
            from datetime import datetime, timezone
            bounty = await db.get(Bounty, inp["bounty_id"])
            if not bounty:
                return SkillOutput(skill=skill_name, version=body.version, status="error", error="Bounty not found")
            if bounty.poster_address != inp["poster_address"]:
                return SkillOutput(skill=skill_name, version=body.version, status="error", error="Only the poster can cancel")
            transition(bounty, "cancelled", reason=inp.get("reason"))
            partial = bounty.ever_delivered
            await refund_escrow(db, bounty, partial=partial)
            bounty.refunded_at = datetime.now(timezone.utc)
            await db.commit()
            return SkillOutput(
                skill=skill_name, version=body.version, status="ok",
                output={
                    "bounty_id": bounty.id, "status": bounty.status,
                    "refunded": True, "partial_refund": partial,
                },
            )

        # ── SAP-8004 Reputation ────────────────────────────

        elif skill_name == "GetReputation":
            from app.services.reputation_service import compute_reputation
            result = await compute_reputation(db, inp["address"])
            return SkillOutput(skill=skill_name, version=body.version, status="ok", output=result)

        # ── Payment Info ───────────────────────────────────

        elif skill_name == "GetPaymentInfo":
            x402_enabled = bool(settings.platform_wallet_address)
            return SkillOutput(
                skill=skill_name, version=body.version, status="ok",
                output={
                    "x402_enabled": x402_enabled,
                    "network": settings.solana_network,
                    "usdc_mint": settings.usdc_mint_address if x402_enabled else None,
                    "platform_wallet": settings.platform_wallet_address if x402_enabled else None,
                    "fee_rates": {"none": 0.05, "platform_ai": 0.15, "custom": 0.15},
                },
            )

    except Exception as e:
        return SkillOutput(skill=skill_name, version=body.version, status="error", error=str(e))

    return SkillOutput(skill=skill_name, version=body.version, status="error", error="Not implemented")
