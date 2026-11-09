"""Authenticated content-category preferences and category catalogue."""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import get_current_user
from app.database import get_db

router = APIRouter(tags=["content category"])

# Stable IDs are persisted and returned by the API; labels are display copy.
CATEGORY_OPTIONS = [
    {"id": "artist", "label": "Artist", "allowsCustom": False},
    {"id": "blogger", "label": "Blogger", "allowsCustom": False},
    {"id": "digital_creator", "label": "Digital Creator", "allowsCustom": False},
    {"id": "education", "label": "Education", "allowsCustom": False},
    {"id": "entertainment", "label": "Entertainment", "allowsCustom": False},
    {"id": "fashion", "label": "Fashion", "allowsCustom": False},
    {"id": "food_lifestyle", "label": "Food & Lifestyle", "allowsCustom": False},
    {"id": "gaming", "label": "Gaming", "allowsCustom": False},
    {"id": "health_fitness", "label": "Health & Fitness", "allowsCustom": False},
    {"id": "music", "label": "Music", "allowsCustom": False},
    {"id": "photography", "label": "Photography", "allowsCustom": False},
    {"id": "technology", "label": "Technology", "allowsCustom": False},
    {"id": "travel", "label": "Travel", "allowsCustom": False},
    {"id": "business", "label": "Business", "allowsCustom": False},
    {"id": "other", "label": "Other", "allowsCustom": True},
]
DEFAULT_CATEGORY = "digital_creator"


def _serialize(row: models.UserContentCategory | None) -> schemas.ContentCategoryOut:
    # The prototype's default selection is Digital Creator. GET remains read-only;
    # the default is persisted only after the user explicitly saves a selection.
    if row is None:
        return schemas.ContentCategoryOut(
            category=DEFAULT_CATEGORY, customCategory=None, updatedAt=None
        )
    return schemas.ContentCategoryOut(
        category=row.category,
        customCategory=row.custom_category,
        updatedAt=row.updated_at,
    )


@router.get("/v1/content-categories", response_model=list[schemas.ContentCategoryOption])
def get_content_categories():
    """Return the supported category IDs and their display labels."""
    return CATEGORY_OPTIONS


@router.get("/v1/me/content-category", response_model=schemas.ContentCategoryOut)
def get_my_content_category(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = db.query(models.UserContentCategory).filter_by(user_id=current_user.id).first()
    return _serialize(row)


@router.put("/v1/me/content-category", response_model=schemas.ContentCategoryOut)
def save_my_content_category(
    payload: schemas.ContentCategorySave,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = db.query(models.UserContentCategory).filter_by(user_id=current_user.id).first()
    old_category = row.category if row else None
    old_custom = row.custom_category if row else None

    if row is None:
        row = models.UserContentCategory(
            user_id=current_user.id,
            category=payload.category,
            custom_category=payload.customCategory,
        )
        db.add(row)
    else:
        row.category = payload.category
        row.custom_category = payload.customCategory

    changed = (
        old_category != payload.category or old_custom != payload.customCategory
    )
    try:
        if changed:
            db.add(models.ContentCategoryAudit(
                user_id=current_user.id,
                old_category=old_category,
                new_category=payload.category,
                old_custom_category=old_custom,
                new_custom_category=payload.customCategory,
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent", "")[:500] or None,
            ))
        db.commit()
        db.refresh(row)
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to save content category",
        )
    return _serialize(row)
