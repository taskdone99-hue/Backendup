from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user
from app.services.monetization_service import (
    get_monetization_status,
    get_earnings_summary,
    list_earnings,
)

router = APIRouter(prefix="/api/monetization", tags=["monetization"])


@router.get("/status", response_model=schemas.MonetizationStatusOut)
def get_status(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Reel watch-time monetization eligibility for the current user —
    calculated dynamically from WatchSession rows on every call (see
    app.services.monetization_service), never persisted, so there's no
    stale-state risk across a period reset and no way for a client to set
    monetization_enabled directly.
    """
    return get_monetization_status(db, current_user.id)


@router.get("/earnings", response_model=schemas.PaginatedCreatorEarningsResponse)
def get_earnings(
    source_type: models.EarningSourceType | None = Query(
        default=None, description="Filter to one earning source"
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Paginated earnings ledger for the current user. Every row was written
    by the system in response to a real event (a brand collaboration or
    creator-collaboration request being accepted) — there is no way to
    credit yourself directly through this or any other endpoint.
    """
    total, total_cents, rows = list_earnings(
        db, current_user.id, source_type=source_type, limit=limit, offset=offset
    )
    currency = rows[0].currency if rows else "INR"
    return schemas.PaginatedCreatorEarningsResponse(
        total=total,
        limit=limit,
        offset=offset,
        total_earnings_cents=total_cents,
        currency=currency,
        items=[schemas.CreatorEarningOut.model_validate(r) for r in rows],
    )


@router.get("/earnings/summary", response_model=schemas.CreatorEarningsSummaryOut)
def get_earnings_summary_endpoint(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Lifetime earnings broken down by source, plus current watch-time
    monetization eligibility (same data as GET /api/monetization/status),
    all in one call for a creator dashboard screen."""
    return get_earnings_summary(db, current_user.id)
