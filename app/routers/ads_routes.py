from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import get_current_user
from app.database import get_db
from app.services import admob_service

# Ads are served by Google AdMob on the client. These endpoints only (a) give
# the app its AdMob IDs per placement/platform and (b) record impression /
# click analytics. No revenue is calculated or stored — AdMob owns that.
# All three require a logged-in user.
router = APIRouter(prefix="/api/ads", tags=["ads"])


@router.get("/config", response_model=schemas.AdMobConfigResponse)
def get_ad_config(
    platform: str = Query(..., pattern="^(android|ios)$", description="android or ios"),
    current_user: models.User = Depends(get_current_user),
):
    return admob_service.build_config(platform)


def _record(model, payload: schemas.AdEventRequest, user: models.User, db: Session, message: str):
    db.add(
        model(
            user_id=user.id,
            ad_id=payload.ad_id,
            placement=payload.placement,
            platform=payload.platform,
            ad_unit_id=payload.ad_unit_id,
        )
    )
    db.commit()
    return schemas.AdEventResponse(
        message=message,
        placement=payload.placement,
        platform=payload.platform,
        ad_unit_id=payload.ad_unit_id,
        ad_id=payload.ad_id,
    )


@router.post("/impression", response_model=schemas.AdEventResponse, status_code=status.HTTP_201_CREATED)
def track_ad_impression(
    payload: schemas.AdEventRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return _record(models.AdImpression, payload, current_user, db, "Impression recorded")


@router.post("/click", response_model=schemas.AdEventResponse, status_code=status.HTTP_201_CREATED)
def track_ad_click(
    payload: schemas.AdEventRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return _record(models.AdClick, payload, current_user, db, "Click recorded")
