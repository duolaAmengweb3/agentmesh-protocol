"""Bounty state machine — task-based A2A exchange.

States:
  open → claimed → delivered → accepted → reviewed
    ↓       ↓          ↓→ revision_requested → delivered (max N)
  expired  (timeout→open) ↓→ rejected (→ open, B kicked)
    ↓
  cancelled (A cancels)

Terminal: expired, cancelled, reviewed
"""

from datetime import datetime, timedelta, timezone

from app.errors import ConflictError, ForbiddenError
from app.models.bounty import Bounty

TRANSITIONS: dict[str, set[str]] = {
    "open": {"claimed", "expired", "cancelled"},
    "claimed": {"delivered", "open", "cancelled"},
    "delivered": {"accepted", "revision_requested", "open", "cancelled"},
    "revision_requested": {"delivered"},
    "accepted": {"reviewed"},
}


def can_transition(current: str, new: str) -> bool:
    return new in TRANSITIONS.get(current, set())


def transition(bounty: Bounty, new_status: str, **kwargs) -> Bounty:
    """Transition bounty to a new status with domain logic.

    kwargs:
      claimer_address: str  — for claim
      claimer_endpoint: str — for claim
      reason: str           — for reject/cancel/revision
      output: dict          — for deliver
    """
    if bounty.status == new_status:
        return bounty

    if not can_transition(bounty.status, new_status):
        raise ConflictError(
            f"Cannot transition from '{bounty.status}' to '{new_status}'",
            detail={"current_status": bounty.status, "requested_status": new_status},
        )

    now = datetime.now(timezone.utc)
    bounty.updated_at = now

    # --- Domain logic per transition ---

    if new_status == "claimed":
        claimer = kwargs.get("claimer_address")
        if not claimer:
            raise ConflictError("claimer_address required for claim")

        # Check not in rejected list
        rejected = bounty.rejected_claimers or []
        if claimer in rejected:
            raise ForbiddenError("You were rejected from this bounty and cannot reclaim")

        # Check assigned_to constraint
        if bounty.assigned_to and bounty.assigned_to != claimer:
            raise ForbiddenError("This bounty is assigned to a specific agent")

        bounty.claimer_address = claimer
        bounty.claimer_endpoint = kwargs.get("claimer_endpoint")
        bounty.claimed_at = now
        bounty.delivery_deadline = now + timedelta(seconds=bounty.sla_seconds)

    elif new_status == "delivered":
        output = kwargs.get("output")
        bounty.output_payload = output
        bounty.delivered_at = now
        bounty.ever_delivered = True
        bounty.acceptance_deadline = now + timedelta(seconds=bounty.acceptance_window_seconds)

    elif new_status == "revision_requested":
        if bounty.revision_count >= bounty.max_revisions:
            raise ConflictError(
                f"Maximum revisions ({bounty.max_revisions}) reached. Must accept or reject.",
                detail={"revision_count": bounty.revision_count, "max_revisions": bounty.max_revisions},
            )
        bounty.revision_count += 1
        bounty.revision_notes = kwargs.get("reason", "")
        bounty.acceptance_deadline = None  # Stop auto-accept timer during revision

    elif new_status == "open" and bounty.status in ("claimed", "delivered", "revision_requested"):
        # Reject claimer / claim timeout → kick B, reset, back to open
        old_claimer = bounty.claimer_address
        if old_claimer:
            rejected = list(bounty.rejected_claimers or [])
            if old_claimer not in rejected:
                rejected.append(old_claimer)
            bounty.rejected_claimers = rejected

        bounty.reject_reason = kwargs.get("reason")
        bounty.claimer_address = None
        bounty.claimer_endpoint = None
        bounty.claimed_at = None
        bounty.delivery_deadline = None
        bounty.acceptance_deadline = None
        bounty.output_payload = None
        bounty.revision_count = 0
        bounty.revision_notes = None
        bounty.delivered_at = None

    elif new_status == "accepted":
        bounty.accepted_at = now
        bounty.completed_at = now

    elif new_status == "cancelled":
        bounty.cancel_reason = kwargs.get("reason")
        bounty.completed_at = now

    elif new_status == "expired":
        bounty.completed_at = now

    elif new_status == "reviewed":
        pass  # Just status change

    bounty.status = new_status
    return bounty
