
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db


router = APIRouter(
    prefix="/api/locations",
    tags=["Locations"],
)


@router.get("/{location_id}/reels")
def get_location_reels(
    location_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    # Verify that the location exists.
    location = (
        db.query(models.Location)
        .filter(models.Location.id == location_id)
        .first()
    )

    if location is None:
        raise HTTPException(
            status_code=404,
            detail="Location not found",
        )

    # Return public reels and the current user's own reels.
    reels = (
        db.query(models.Reel)
        .filter(
            models.Reel.location_id == location_id,
            or_(
                models.Reel.visibility == "public",
                models.Reel.user_id == current_user.id,
            ),
        )
        .order_by(models.Reel.created_at.desc())
        .all()
    )

    return {
        "location_id": location.id,
        "location_name": location.name,
        "total": len(reels),
        "items": [
            {
                "id": reel.id,
                "user_id": reel.user_id,
                "location_id": reel.location_id,
                "visibility": reel.visibility,
                "created_at": reel.created_at,
            }
            for reel in reels
        ],
    }
