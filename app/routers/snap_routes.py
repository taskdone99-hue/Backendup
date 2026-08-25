"""
Snap / Camera & Filters. The AR filter catalog is a small static list served
straight from this router rather than a DB table (see models.Snap docstring
for why `filter_id` isn't a ForeignKey) — swap `FILTER_CATALOG` for a real
table if filters ever need to be managed dynamically.
"""

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user
from app.services.media_service import save_upload_file

router = APIRouter(tags=["snaps"])

FILTER_CATALOG: list[schemas.FilterOut] = [
    schemas.FilterOut(id="none", name="Normal", category="basic"),
    schemas.FilterOut(id="grayscale", name="Grayscale", category="basic"),
    schemas.FilterOut(id="sepia", name="Sepia", category="basic"),
    schemas.FilterOut(id="dog_ears", name="Dog Ears", category="face"),
    schemas.FilterOut(id="flower_crown", name="Flower Crown", category="face"),
    schemas.FilterOut(id="sparkles", name="Sparkles", category="fun"),
    schemas.FilterOut(id="vintage_film", name="Vintage Film", category="tone"),
    schemas.FilterOut(id="neon_glow", name="Neon Glow", category="fun"),
]

FILTER_IDS = {f.id for f in FILTER_CATALOG}


@router.get("/api/filters", response_model=schemas.FiltersResponse)
def list_filters():
    return schemas.FiltersResponse(items=FILTER_CATALOG)


@router.post("/api/snaps", response_model=schemas.SnapOut, status_code=status.HTTP_201_CREATED)
def upload_snap(
    file: UploadFile,
    filter_id: str | None = Form(default=None),
    caption: str | None = Form(default=None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if filter_id is not None and filter_id not in FILTER_IDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown filter_id '{filter_id}'. See GET /api/filters for valid ids.",
        )

    url, kind = save_upload_file(file, "snaps", allow_video=True)

    snap = models.Snap(
        user_id=current_user.id,
        media_url=url,
        media_type=models.MediaType.video if kind == "video" else models.MediaType.image,
        filter_id=filter_id,
        caption=caption,
    )
    db.add(snap)
    db.commit()
    db.refresh(snap)

    return snap
