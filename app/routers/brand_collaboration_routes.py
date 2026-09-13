"""
Brand (paid-partnership) collaboration offers — a brand rep offers a
sponsorship deal to a creator; the creator accepts/rejects it, and
acceptance automatically credits the creator's earnings ledger.

See app/services/brand_collaboration_service.py for the business logic and
app.models.BrandCollaboration for why there's no separate "Brand" account
type.
"""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user
from app.services import brand_collaboration_service

router = APIRouter(prefix="/api/brand-collaborations", tags=["brand-collaborations"])


def _to_out(offer: models.BrandCollaboration) -> schemas.BrandCollaborationOut:
    return schemas.BrandCollaborationOut(
        id=offer.id,
        created_by=schemas.UserSummaryOut.model_validate(offer.created_by),
        creator=schemas.UserSummaryOut.model_validate(offer.creator),
        brand_name=offer.brand_name,
        brand_contact_email=offer.brand_contact_email,
        campaign_title=offer.campaign_title,
        campaign_description=offer.campaign_description,
        offer_amount_cents=offer.offer_amount_cents,
        currency=offer.currency,
        deliverables=offer.deliverables,
        status=offer.status,
        created_at=offer.created_at,
        responded_at=offer.responded_at,
    )


@router.post("", response_model=schemas.BrandCollaborationOut, status_code=status.HTTP_201_CREATED)
def create_brand_collaboration(
    payload: schemas.BrandCollaborationCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Create a brand sponsorship offer targeting a creator. The
    authenticated caller is treated as the brand's representative
    (`created_by`) — see the BrandCollaboration model docstring."""
    offer = brand_collaboration_service.create_offer(
        db,
        created_by_id=current_user.id,
        creator_user_id=payload.creator_user_id,
        brand_name=payload.brand_name,
        brand_contact_email=payload.brand_contact_email,
        campaign_title=payload.campaign_title,
        campaign_description=payload.campaign_description,
        offer_amount_cents=payload.offer_amount_cents,
        currency=payload.currency,
        deliverables=payload.deliverables,
    )
    return _to_out(offer)


@router.get("", response_model=schemas.PaginatedBrandCollaborationResponse)
def list_brand_collaborations(
    role: str = Query("all", pattern="^(all|creator|brand)$"),
    offer_status: models.BrandCollaborationStatus | None = Query(default=None, alias="status"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """`role`: creator (offers where you're the target creator), brand
    (offers you created as a brand rep), or all (default)."""
    total, rows = brand_collaboration_service.list_offers(
        db,
        current_user.id,
        role=role,
        status_filter=offer_status,
        limit=limit,
        offset=offset,
    )
    return schemas.PaginatedBrandCollaborationResponse(
        total=total, limit=limit, offset=offset, items=[_to_out(o) for o in rows]
    )


@router.get("/{offer_id}", response_model=schemas.BrandCollaborationOut)
def get_brand_collaboration(
    offer_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    offer = brand_collaboration_service.get_offer_or_404(db, offer_id)
    brand_collaboration_service.assert_participant(offer, current_user.id)
    return _to_out(offer)


@router.post("/{offer_id}/accept", response_model=schemas.BrandCollaborationOut)
def accept_brand_collaboration(
    offer_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    offer = brand_collaboration_service.get_offer_or_404(db, offer_id)
    offer = brand_collaboration_service.accept_offer(db, offer, current_user.id)
    return _to_out(offer)


@router.post("/{offer_id}/reject", response_model=schemas.BrandCollaborationOut)
def reject_brand_collaboration(
    offer_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    offer = brand_collaboration_service.get_offer_or_404(db, offer_id)
    offer = brand_collaboration_service.reject_offer(db, offer, current_user.id)
    return _to_out(offer)


@router.post("/{offer_id}/cancel", response_model=schemas.BrandCollaborationOut)
def cancel_brand_collaboration(
    offer_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    offer = brand_collaboration_service.get_offer_or_404(db, offer_id)
    offer = brand_collaboration_service.cancel_offer(db, offer, current_user.id)
    return _to_out(offer)


@router.post("/{offer_id}/complete", response_model=schemas.BrandCollaborationOut)
def complete_brand_collaboration(
    offer_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    offer = brand_collaboration_service.get_offer_or_404(db, offer_id)
    offer = brand_collaboration_service.complete_offer(db, offer, current_user.id)
    return _to_out(offer)
