from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user
from app.services.monetization_service import get_monetization_status

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
