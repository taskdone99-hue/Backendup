"""
Story Highlights: pinned, non-expiring collections of story content on a
profile. Adding a story to a highlight copies its media into a HighlightItem
snapshot (see models.HighlightItem) so the highlight survives the source
story's expiry.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user

router = APIRouter(tags=["highlights"])


# ---- internal helpers ----

def _get_highlight_or_404(db: Session, highlight_id: int) -> models.Highlight:
    highlight = (
        db.query(models.Highlight).filter(models.Highlight.id == highlight_id).first()
    )
    if highlight is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Highlight not found")
    return highlight


def _require_owner(highlight: models.Highlight, current_user: models.User) -> None:
    if highlight.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only manage your own highlights",
        )


def _get_active_story_or_404(db: Session, story_id: int) -> models.Story:
    now = datetime.now(timezone.utc)
    story = (
        db.query(models.Story)
        .filter(models.Story.id == story_id, models.Story.expires_at > now)
        .first()
    )
    if story is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Story {story_id} not found or has expired",
        )
    return story


def _to_highlight_out(highlight: models.Highlight) -> schemas.HighlightOut:
    out = schemas.HighlightOut.model_validate(highlight)
    out.items_count = len(highlight.items)
    return out


def _to_highlight_detail(highlight: models.Highlight) -> schemas.HighlightDetailOut:
    detail = schemas.HighlightDetailOut.model_validate(highlight)
    detail.items = [schemas.HighlightItemOut.model_validate(i) for i in highlight.items]
    detail.items_count = len(highlight.items)
    return detail


# ==========================================================================
# Story Highlights
# ==========================================================================

@router.post(
    "/api/highlights", response_model=schemas.HighlightDetailOut, status_code=status.HTTP_201_CREATED
)
def create_highlight(
    payload: schemas.HighlightCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    highlight = models.Highlight(
        user_id=current_user.id,
        title=payload.title,
        cover_url=payload.cover_url,
    )
    db.add(highlight)
    db.flush()  # assigns highlight.id without committing yet

    for story_id in payload.story_ids:
        story = _get_active_story_or_404(db, story_id)
        if story.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You can only add your own stories to a highlight (story {story_id})",
            )
        db.add(
            models.HighlightItem(
                highlight_id=highlight.id,
                source_story_id=story.id,
                media_url=story.media_url,
                media_type=story.media_type,
                caption=story.caption,
            )
        )

    if highlight.cover_url is None and highlight.items:
        highlight.cover_url = highlight.items[0].media_url

    db.commit()
    db.refresh(highlight)
    return _to_highlight_detail(highlight)


@router.get("/api/users/{user_id}/highlights", response_model=schemas.HighlightsListResponse)
def get_user_highlights(user_id: int, db: Session = Depends(get_db)):
    highlights = (
        db.query(models.Highlight)
        .filter(models.Highlight.user_id == user_id)
        .order_by(models.Highlight.created_at.desc())
        .all()
    )
    return schemas.HighlightsListResponse(items=[_to_highlight_out(h) for h in highlights])


@router.get("/api/highlights/{highlight_id}", response_model=schemas.HighlightDetailOut)
def get_highlight(highlight_id: int, db: Session = Depends(get_db)):
    highlight = _get_highlight_or_404(db, highlight_id)
    return _to_highlight_detail(highlight)


@router.put("/api/highlights/{highlight_id}", response_model=schemas.HighlightDetailOut)
def update_highlight(
    highlight_id: int,
    payload: schemas.HighlightUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    highlight = _get_highlight_or_404(db, highlight_id)
    _require_owner(highlight, current_user)

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(highlight, field, value)

    db.commit()
    db.refresh(highlight)
    return _to_highlight_detail(highlight)


@router.delete("/api/highlights/{highlight_id}", response_model=schemas.MessageResponse)
def delete_highlight(
    highlight_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    highlight = _get_highlight_or_404(db, highlight_id)
    _require_owner(highlight, current_user)

    db.delete(highlight)
    db.commit()
    return schemas.MessageResponse(message="Highlight deleted")


@router.post("/api/highlights/{highlight_id}/stories", response_model=schemas.HighlightDetailOut)
def add_stories_to_highlight(
    highlight_id: int,
    payload: schemas.AddHighlightStoriesRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    highlight = _get_highlight_or_404(db, highlight_id)
    _require_owner(highlight, current_user)

    for story_id in payload.story_ids:
        story = _get_active_story_or_404(db, story_id)
        if story.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You can only add your own stories to a highlight (story {story_id})",
            )
        db.add(
            models.HighlightItem(
                highlight_id=highlight.id,
                source_story_id=story.id,
                media_url=story.media_url,
                media_type=story.media_type,
                caption=story.caption,
            )
        )

    db.commit()
    db.refresh(highlight)
    return _to_highlight_detail(highlight)


@router.delete(
    "/api/highlights/{highlight_id}/stories/{item_id}", response_model=schemas.MessageResponse
)
def remove_highlight_item(
    highlight_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    highlight = _get_highlight_or_404(db, highlight_id)
    _require_owner(highlight, current_user)

    item = (
        db.query(models.HighlightItem)
        .filter(
            models.HighlightItem.id == item_id,
            models.HighlightItem.highlight_id == highlight_id,
        )
        .first()
    )
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Highlight item not found")

    db.delete(item)
    db.commit()
    return schemas.MessageResponse(message="Removed from highlight")
