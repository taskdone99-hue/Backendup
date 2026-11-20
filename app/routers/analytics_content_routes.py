"""Creator analytics content picker across posts, reels and long-form videos.

This endpoint powers the searchable content list in the Analytics UI. Detailed
Reel analytics remain available from reel_insights_routes; this picker does not
pretend that post/video playback analytics exist where no event source is stored.
"""
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db

router = APIRouter(prefix="/api/analytics", tags=["Creator Analytics"])
ContentType = Literal["all", "post", "reel", "video"]


def _post_item(row: models.Post) -> dict:
    return {
        "content_type": "post", "id": row.id, "title": None,
        "caption": row.caption, "media_url": row.media_url,
        "thumbnail_url": row.media_url, "created_at": row.created_at,
    }


def _reel_item(row: models.Reel) -> dict:
    return {
        "content_type": "reel", "id": row.id, "title": None,
        "caption": row.caption, "media_url": row.video_url,
        "thumbnail_url": row.thumbnail_url, "created_at": row.created_at,
    }


def _video_item(row: models.Video) -> dict:
    return {
        "content_type": "video", "id": row.id, "title": row.title,
        "caption": row.description, "media_url": row.video_url,
        "thumbnail_url": row.thumbnail_url, "created_at": row.created_at,
    }


@router.get("/content", summary="Search my posts, reels and videos for analytics")
def search_analytics_content(
    q: str | None = Query(default=None, max_length=150, description="Search title or caption"),
    content_type: ContentType = Query(default="all"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Return the authenticated creator's content, newest first, with search.

    Pagination is applied after merging the per-type result sets so mixed
    content has one consistent newest-first ordering.
    """
    needle = (q or "").strip()
    pattern = f"%{needle}%" if needle else None
    items: list[dict] = []
    total = 0
    start = (page - 1) * limit
    # Fetch only enough rows from each content type to construct the requested
    # merged page; this avoids loading a creator's entire library into memory.
    fetch_limit = start + limit

    if content_type in ("all", "post"):
        query = db.query(models.Post).filter(models.Post.user_id == user.id)
        if pattern:
            query = query.filter(models.Post.caption.ilike(pattern))
        total += query.with_entities(func.count(models.Post.id)).scalar() or 0
        items.extend(_post_item(row) for row in query.order_by(models.Post.created_at.desc(), models.Post.id.desc()).limit(fetch_limit).all())

    if content_type in ("all", "reel"):
        query = db.query(models.Reel).filter(models.Reel.user_id == user.id)
        if pattern:
            query = query.filter(models.Reel.caption.ilike(pattern))
        total += query.with_entities(func.count(models.Reel.id)).scalar() or 0
        items.extend(_reel_item(row) for row in query.order_by(models.Reel.created_at.desc(), models.Reel.id.desc()).limit(fetch_limit).all())

    if content_type in ("all", "video"):
        query = db.query(models.Video).filter(models.Video.user_id == user.id)
        if pattern:
            query = query.filter(or_(models.Video.title.ilike(pattern), models.Video.description.ilike(pattern)))
        total += query.with_entities(func.count(models.Video.id)).scalar() or 0
        items.extend(_video_item(row) for row in query.order_by(models.Video.created_at.desc(), models.Video.id.desc()).limit(fetch_limit).all())

    items.sort(key=lambda item: (item["created_at"] is not None, item["created_at"], item["id"]), reverse=True)
    selected = items[start:start + limit]
    return {
        "items": selected,
        "pagination": {
            "page": page, "limit": limit, "total": total,
            "total_pages": (total + limit - 1) // limit,
            "has_next": start + limit < total,
            "has_previous": page > 1,
        },
        "filters": {"query": needle or None, "content_type": content_type},
        "supported_content_types": ["post", "reel", "video"],
    }
