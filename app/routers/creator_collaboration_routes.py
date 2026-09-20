"""
Creator-to-creator collaboration requests — invite a fellow creator to
collaborate (optionally on a specific reel you own, optionally offering a
revenue share and/or a flat payout), and accept/reject/cancel the request.

See app/services/collaboration_service.py for the business logic and
app.models.CreatorCollaborationRequest for how this relates to the existing
ReelCollaborator/ReelRevenueShare tables.
"""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user
from app.services import collaboration_service

router = APIRouter(prefix="/api/creator-collaborations", tags=["creator-collaborations"])


def _to_out(request: models.CreatorCollaborationRequest) -> schemas.CreatorCollaborationOut:
    return schemas.CreatorCollaborationOut(
        id=request.id,
        requester=schemas.UserSummaryOut.model_validate(request.requester),
        partner=schemas.UserSummaryOut.model_validate(request.partner),
        reel_id=request.reel_id,
        message=request.message,
        proposed_revenue_share_percentage=request.proposed_revenue_share_percentage,
        proposed_amount_cents=request.proposed_amount_cents,
        status=request.status,
        created_at=request.created_at,
        responded_at=request.responded_at,
    )


@router.post("", response_model=schemas.CreatorCollaborationOut, status_code=status.HTTP_201_CREATED)
async def create_collaboration_request(
    payload: schemas.CreatorCollaborationCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    request = await collaboration_service.create_request(
        db,
        requester=current_user,
        partner_user_id=payload.partner_user_id,
        reel_id=payload.reel_id,
        message=payload.message,
        proposed_revenue_share_percentage=payload.proposed_revenue_share_percentage,
        proposed_amount_cents=payload.proposed_amount_cents,
    )
    return _to_out(request)


@router.get("", response_model=schemas.PaginatedCreatorCollaborationResponse)
def list_collaboration_requests(
    direction: str = Query("all", pattern="^(all|incoming|outgoing)$"),
    request_status: models.CollaborationStatus | None = Query(default=None, alias="status"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """`direction`: incoming (you're the invited partner), outgoing (you
    sent the request), or all (default)."""
    total, rows = collaboration_service.list_requests(
        db,
        current_user.id,
        direction=direction,
        status_filter=request_status,
        limit=limit,
        offset=offset,
    )
    return schemas.PaginatedCreatorCollaborationResponse(
        total=total, limit=limit, offset=offset, items=[_to_out(r) for r in rows]
    )


@router.get("/{request_id}", response_model=schemas.CreatorCollaborationOut)
def get_collaboration_request(
    request_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    request = collaboration_service.get_request_or_404(db, request_id)
    collaboration_service.assert_participant(request, current_user.id)
    return _to_out(request)


@router.post("/{request_id}/accept", response_model=schemas.CreatorCollaborationOut)
async def accept_collaboration_request(
    request_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    request = collaboration_service.get_request_or_404(db, request_id)
    request = await collaboration_service.accept_request(db, request, current_user)
    return _to_out(request)


@router.post("/{request_id}/reject", response_model=schemas.CreatorCollaborationOut)
async def reject_collaboration_request(
    request_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    request = collaboration_service.get_request_or_404(db, request_id)
    request = await collaboration_service.reject_request(db, request, current_user)
    return _to_out(request)


@router.post("/{request_id}/cancel", response_model=schemas.CreatorCollaborationOut)
async def cancel_collaboration_request(
    request_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    request = collaboration_service.get_request_or_404(db, request_id)
    request = await collaboration_service.cancel_request(db, request, current_user)
    return _to_out(request)
