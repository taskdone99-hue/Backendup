import os

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import bearer_scheme_optional, get_user_from_raw_token

router = APIRouter(prefix="/api/ads", tags=["ads"])

AD_NETWORK = os.getenv("AD_NETWORK")  # e.g. "admob", "meta_audience_network"
AD_TEST_MODE = os.getenv("AD_TEST_MODE", "true").lower() == "true"

# Static for now — how often an ad slot appears in each feed, and whether
# it's turned on at all. Move to the DB/an admin panel if these need to be
# changed without a deploy.
_AD_SLOTS = [
    {"placement": "feed", "enabled": True, "frequency": 6},
    {"placement": "explore", "enabled": True, "frequency": 9},
    {"placement": "reels", "enabled": True, "frequency": 5},
    {"placement": "stories", "enabled": False, "frequency": 4},
]


@router.post("/impression", response_model=schemas.AdImpressionResponse, status_code=status.HTTP_201_CREATED)
def track_ad_impression(
    payload: schemas.AdImpressionRequest,
    db: Session = Depends(get_db),
    credentials=Depends(bearer_scheme_optional),
):
    # Impressions are accepted for logged-out users too (e.g. a preview
    # feed) — attribute to a user only when a valid token was supplied.
    user = None
    if credentials is not None:
        user = get_user_from_raw_token(credentials.credentials, db)

    impression = models.AdImpression(
        user_id=user.id if user else None,
        ad_id=payload.ad_id,
        placement=payload.placement,
    )
    db.add(impression)
    db.commit()

    return schemas.AdImpressionResponse(message="Impression recorded", ad_id=payload.ad_id, placement=payload.placement)


@router.get("/config", response_model=schemas.AdConfigResponse)
def get_ad_config():
    return schemas.AdConfigResponse(
        ad_network=AD_NETWORK,
        test_mode=AD_TEST_MODE,
        slots=[schemas.AdSlotOut(**slot) for slot in _AD_SLOTS],
    )
