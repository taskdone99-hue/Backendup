"""
Brand (paid-partnership) collaboration offers — a brand's sponsorship offer
to a creator, accepted/declined much like the creator-to-creator requests in
collaboration_service, but crediting CreatorEarning on acceptance since a
brand deal is a real monetary offer rather than just a co-creation tag.

There's no separate authenticated "brand" account type in this codebase, so
an offer is created by whichever authenticated user is acting as the
brand's rep (see models.BrandCollaboration docstring) and identifies itself
via brand_name/contact fields.
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


def create_offer(
    db: Session,
    *,
    created_by_id: int,
    creator_user_id: int,
    brand_name: str,
    brand_contact_email: str | None,
    campaign_title: str,
    campaign_description: str | None,
    offer_amount_cents: int,
    currency: str,
    deliverables: str | None,
) -> models.BrandCollaboration:
    if creator_user_id == created_by_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You can't create a brand offer targeting yourself",
        )

    creator = (
        db.query(models.User)
        .filter(models.User.id == creator_user_id, models.User.is_active.is_(True))
        .first()
    )
    if creator is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Creator not found")

    offer = models.BrandCollaboration(
        created_by_id=created_by_id,
        creator_id=creator_user_id,
        brand_name=brand_name,
        brand_contact_email=brand_contact_email,
        campaign_title=campaign_title,
        campaign_description=campaign_description,
        offer_amount_cents=offer_amount_cents,
        currency=currency,
        deliverables=deliverables,
        status=models.BrandCollaborationStatus.pending,
    )
    db.add(offer)
    db.commit()
    db.refresh(offer)
    return offer


def get_offer_or_404(db: Session, offer_id: int) -> models.BrandCollaboration:
    offer = db.query(models.BrandCollaboration).filter(models.BrandCollaboration.id == offer_id).first()
    if offer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brand collaboration not found")
    return offer


def assert_participant(offer: models.BrandCollaboration, user_id: int) -> None:
    if user_id not in (offer.created_by_id, offer.creator_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not part of this brand collaboration")


def list_offers(
    db: Session,
    user_id: int,
    *,
    role: str = "all",
    status_filter: models.BrandCollaborationStatus | None = None,
    limit: int = 20,
    offset: int = 0,
):
    query = db.query(models.BrandCollaboration)
    if role == "creator":
        query = query.filter(models.BrandCollaboration.creator_id == user_id)
    elif role == "brand":
        query = query.filter(models.BrandCollaboration.created_by_id == user_id)
    else:
        query = query.filter(
            or_(
                models.BrandCollaboration.creator_id == user_id,
                models.BrandCollaboration.created_by_id == user_id,
            )
        )

    if status_filter is not None:
        query = query.filter(models.BrandCollaboration.status == status_filter)

    total = query.count()
    rows = (
        query.order_by(models.BrandCollaboration.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return total, rows


def accept_offer(db: Session, offer: models.BrandCollaboration, current_user_id: int) -> models.BrandCollaboration:
    if offer.creator_id != current_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the targeted creator can accept this offer")
    if offer.status != models.BrandCollaborationStatus.pending:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Offer is already {offer.status.value}")

    offer.status = models.BrandCollaborationStatus.accepted
    offer.responded_at = _now()

    record_earning(
        db,
        user_id=offer.creator_id,
        source_type=models.EarningSourceType.brand_collaboration,
        source_id=offer.id,
        amount_cents=offer.offer_amount_cents,
        currency=offer.currency,
        description=f"Brand deal with {offer.brand_name}: {offer.campaign_title}",
    )

    db.commit()
    db.refresh(offer)
    return offer


def reject_offer(db: Session, offer: models.BrandCollaboration, current_user_id: int) -> models.BrandCollaboration:
    if offer.creator_id != current_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the targeted creator can reject this offer")
    if offer.status != models.BrandCollaborationStatus.pending:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Offer is already {offer.status.value}")

    offer.status = models.BrandCollaborationStatus.rejected
    offer.responded_at = _now()
    db.commit()
    db.refresh(offer)
    return offer


def cancel_offer(db: Session, offer: models.BrandCollaboration, current_user_id: int) -> models.BrandCollaboration:
    if offer.created_by_id != current_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the offer's creator (brand rep) can cancel it")
    if offer.status != models.BrandCollaborationStatus.pending:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Offer is already {offer.status.value}")

    offer.status = models.BrandCollaborationStatus.cancelled
    offer.responded_at = _now()
    db.commit()
    db.refresh(offer)
    return offer


def complete_offer(db: Session, offer: models.BrandCollaboration, current_user_id: int) -> models.BrandCollaboration:
    """Mark an accepted campaign as completed — either party may do so."""
    assert_participant(offer, current_user_id)
    if offer.status != models.BrandCollaborationStatus.accepted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only an accepted offer can be marked completed",
        )

    offer.status = models.BrandCollaborationStatus.completed
    db.commit()
    db.refresh(offer)
    return offer
