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
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app import models
from app.services import privacy_service
from app.services.monetization_service import record_earning
from app.services.notification_service import notify_user


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _assert_can_reach_partner(db: Session, requester: models.User, partner: models.User) -> None:
    """Who you're allowed to propose a collaboration to.

    Public partner  -> anyone can ask, no follow relationship needed.
    Private partner -> only an *approved* follower can ask. A pending
                       FollowRequest doesn't count: the partner hasn't let
                       the requester in yet, so surfacing a collaboration
                       invite to them would leak around the private wall.

    A block in either direction hides the partner from the requester
    everywhere else in the app, so it blocks this too — and it's checked
    first so the error can't be used to detect a block.
    """
    if privacy_service.is_blocked(db, requester.id, partner.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Partner user not found"
        )

    if not partner.is_private:
        return

    is_follower = (
        db.query(models.Follow)
        .filter(
            models.Follow.follower_id == requester.id,
            models.Follow.following_id == partner.id,
        )
        .first()
        is not None
    )
    if not is_follower:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"@{partner.username} has a private account — you need to be "
                "following them before you can send a collaboration request"
            ),
        )


def _assert_not_duplicate(
    db: Session, *, requester_id: int, partner_id: int, reel_id: int | None
) -> None:
    """One open proposal per pair, per reel.

    Checked in both directions: if the partner has already invited the
    requester, the requester should answer that one rather than open a
    competing thread that both sides could accept separately.

    Scoped by reel_id so a general "let's work together" proposal and a
    per-reel one can coexist, while two invites on the same reel (or two
    general ones) can't. Resolved requests — rejected/cancelled — never
    block a fresh attempt.
    """
    reel_clause = (
        models.CreatorCollaborationRequest.reel_id.is_(None)
        if reel_id is None
        else models.CreatorCollaborationRequest.reel_id == reel_id
    )
    existing = (
        db.query(models.CreatorCollaborationRequest)
        .filter(
            models.CreatorCollaborationRequest.status == models.CollaborationStatus.pending,
            reel_clause,
            or_(
                and_(
                    models.CreatorCollaborationRequest.requester_id == requester_id,
                    models.CreatorCollaborationRequest.partner_id == partner_id,
                ),
                and_(
                    models.CreatorCollaborationRequest.requester_id == partner_id,
                    models.CreatorCollaborationRequest.partner_id == requester_id,
                ),
            ),
        )
        .first()
    )
    if existing is not None:
        detail = (
            "You already have a pending collaboration request with this creator"
            if existing.requester_id == requester_id
            else "This creator has already sent you a collaboration request — respond to that one instead"
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

    # Already a tagged co-creator on this reel — nothing left to propose.
    if reel_id is not None:
        already_tagged = (
            db.query(models.ReelCollaborator)
            .filter(
                models.ReelCollaborator.reel_id == reel_id,
                models.ReelCollaborator.user_id == partner_id,
            )
            .first()
        )
        if already_tagged is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This creator is already a collaborator on that reel",
            )


async def create_request(
    db: Session,
    *,
    requester: models.User,
    partner_user_id: int,
    reel_id: int | None,
    message: str | None,
    proposed_revenue_share_percentage: int | None,
    proposed_amount_cents: int | None,
) -> models.CreatorCollaborationRequest:
    requester_id = requester.id

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

    _assert_can_reach_partner(db, requester, partner)

    if reel_id is not None:
        reel = db.query(models.Reel).filter(models.Reel.id == reel_id).first()
        if reel is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reel not found")
        if reel.user_id != requester_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only propose a collaboration on a reel you own",
            )

    _assert_not_duplicate(
        db, requester_id=requester_id, partner_id=partner_user_id, reel_id=reel_id
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

    # Fan out to the partner (DB row + WebSocket + FCM push). Same pattern
    # as a follow request — notify_user swallows WS/push failures itself, so
    # the request is never lost just because delivery hiccuped.
    await notify_user(
        db,
        user_id=partner.id,
        actor=requester,
        notif_type=models.NotificationType.collaboration_request,
        message=f"{requester.username} invited you to collaborate",
        target_type="collab_request",
        target_id=request.id,
    )

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


async def accept_request(
    db: Session, request: models.CreatorCollaborationRequest, responder: models.User
) -> models.CreatorCollaborationRequest:
    current_user_id = responder.id
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

    await notify_user(
        db,
        user_id=request.requester_id,
        actor=responder,
        notif_type=models.NotificationType.collaboration_accepted,
        message=f"{responder.username} accepted your collaboration request",
        target_type="collab_request",
        target_id=request.id,
    )

    db.refresh(request)
    return request


async def reject_request(
    db: Session, request: models.CreatorCollaborationRequest, responder: models.User
) -> models.CreatorCollaborationRequest:
    current_user_id = responder.id
    if request.partner_id != current_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the invited partner can reject this request")
    if request.status != models.CollaborationStatus.pending:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Request is already {request.status.value}")

    request.status = models.CollaborationStatus.rejected
    request.responded_at = _now()
    db.commit()
    db.refresh(request)

    await notify_user(
        db,
        user_id=request.requester_id,
        actor=responder,
        notif_type=models.NotificationType.collaboration_rejected,
        message=f"{responder.username} declined your collaboration request",
        target_type="collab_request",
        target_id=request.id,
    )

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
