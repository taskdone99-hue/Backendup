"""
Creator-to-creator collaboration request workflow — the invite/accept step
that happens before (optionally) tagging someone as a ReelCollaborator.

This is deliberately a separate, lightweight table
(CreatorCollaborationRequest) rather than a change to ReelCollaborator —
ReelCollaborator already means "this person IS a tagged co-creator on this
reel" and is written directly by POST /api/videos/:id/collaborators. This
module models the *proposal* that precedes that — "would you like to
collaborate with me" — with a pending/accepted/rejected/cancelled lifecycle,
and only touches ReelCollaborator/CreatorEarning as a side effect of
acceptance.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import models
from app.services.monetization_service import record_earning


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_request(
    db: Session,
    *,
    requester_id: int,
    partner_user_id: int,
    reel_id: int | None,
    message: str | None,
    proposed_revenue_share_percentage: int | None,
    proposed_amount_cents: int | None,
) -> models.CreatorCollaborationRequest:
    if partner_user_id == requester_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You can't send a collaboration request to yourself",
        )

    partner = (
        db.query(models.User)
        .filter(models.User.id == partner_user_id, models.User.is_active.is_(True))
        .first()
    )
    if partner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Partner user not found")

    if reel_id is not None:
        reel = db.query(models.Reel).filter(models.Reel.id == reel_id).first()
        if reel is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reel not found")
        if reel.user_id != requester_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only propose a collaboration on a reel you own",
            )

    request = models.CreatorCollaborationRequest(
        requester_id=requester_id,
        partner_id=partner_user_id,
        reel_id=reel_id,
        message=message,
        proposed_revenue_share_percentage=proposed_revenue_share_percentage,
        proposed_amount_cents=proposed_amount_cents,
        status=models.CollaborationStatus.pending,
    )
    db.add(request)
    db.commit()
    db.refresh(request)
    return request


def get_request_or_404(db: Session, request_id: int) -> models.CreatorCollaborationRequest:
    request = (
        db.query(models.CreatorCollaborationRequest)
        .filter(models.CreatorCollaborationRequest.id == request_id)
        .first()
    )
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collaboration request not found")
    return request


def assert_participant(request: models.CreatorCollaborationRequest, user_id: int) -> None:
    if user_id not in (request.requester_id, request.partner_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not part of this collaboration request")


def list_requests(
    db: Session,
    user_id: int,
    *,
    direction: str = "all",
    status_filter: models.CollaborationStatus | None = None,
    limit: int = 20,
    offset: int = 0,
):
    query = db.query(models.CreatorCollaborationRequest)
    if direction == "incoming":
        query = query.filter(models.CreatorCollaborationRequest.partner_id == user_id)
    elif direction == "outgoing":
        query = query.filter(models.CreatorCollaborationRequest.requester_id == user_id)
    else:
        query = query.filter(
            or_(
                models.CreatorCollaborationRequest.requester_id == user_id,
                models.CreatorCollaborationRequest.partner_id == user_id,
            )
        )

    if status_filter is not None:
        query = query.filter(models.CreatorCollaborationRequest.status == status_filter)

    total = query.count()
    rows = (
        query.order_by(models.CreatorCollaborationRequest.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return total, rows


def accept_request(db: Session, request: models.CreatorCollaborationRequest, current_user_id: int) -> models.CreatorCollaborationRequest:
    if request.partner_id != current_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the invited partner can accept this request")
    if request.status != models.CollaborationStatus.pending:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Request is already {request.status.value}")

    request.status = models.CollaborationStatus.accepted
    request.responded_at = _now()

    # Side effect 1: if this proposal referenced a reel, tag the partner as
    # a ReelCollaborator on it — reusing the existing table instead of a
    # second "who's on this reel" list. Idempotent: skip if already tagged.
    if request.reel_id is not None:
        already_tagged = (
            db.query(models.ReelCollaborator)
            .filter(
                models.ReelCollaborator.reel_id == request.reel_id,
                models.ReelCollaborator.user_id == request.partner_id,
            )
            .first()
        )
        if already_tagged is None:
            db.add(models.ReelCollaborator(reel_id=request.reel_id, user_id=request.partner_id))

    # Side effect 2: credit the partner with the agreed flat payout, if any.
    if request.proposed_amount_cents:
        requester = db.query(models.User).filter(models.User.id == request.requester_id).first()
        record_earning(
            db,
            user_id=request.partner_id,
            source_type=models.EarningSourceType.creator_collaboration,
            source_id=request.id,
            amount_cents=request.proposed_amount_cents,
            description=f"Collaboration payout from @{requester.username}" if requester else "Creator collaboration payout",
        )

    db.commit()
    db.refresh(request)
    return request


def reject_request(db: Session, request: models.CreatorCollaborationRequest, current_user_id: int) -> models.CreatorCollaborationRequest:
    if request.partner_id != current_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the invited partner can reject this request")
    if request.status != models.CollaborationStatus.pending:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Request is already {request.status.value}")

    request.status = models.CollaborationStatus.rejected
    request.responded_at = _now()
    db.commit()
    db.refresh(request)
    return request


def cancel_request(db: Session, request: models.CreatorCollaborationRequest, current_user_id: int) -> models.CreatorCollaborationRequest:
    if request.requester_id != current_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the requester can cancel this request")
    if request.status != models.CollaborationStatus.pending:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Request is already {request.status.value}")

    request.status = models.CollaborationStatus.cancelled
    request.responded_at = _now()
    db.commit()
    db.refresh(request)
    return request
