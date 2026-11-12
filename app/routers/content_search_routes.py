
"""Creator-owned content search for the Content screen.

GET /api/content/search?q=...&content_type=post|video|reel&page=1&limit=10

Posts are restricted to image media.
Video uploads and Reels currently share the same database table, so they
cannot be distinguished until the schema has a dedicated content-type field.
"""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, or_, exists
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db

router = APIRouter(prefix="/api/content", tags=["Content Search"])


def _media_type_value(media_type) -> str:
    """Convert an enum or string media type to a string."""
    return media_type.value if hasattr(media_type, "value") else str(media_type)


def _image_media(post: models.Post) -> list[dict]:
    """Return image items only, including legacy single-media posts."""
    if post.media_items:
        return [
            {
                "id": item.id,
                "media_url": item.media_url,
                "media_type": _media_type_value(item.media_type),
                "position": item.position,
                "thumbnail_url": item.thumbnail_url,
            }
            for item in post.media_items
            if _media_type_value(item.media_type) == "image"
        ]

    if _media_type_value(post.media_type) == "image":
        return [
            {
                "id": post.id,
                "media_url": post.media_url,
                "media_type": "image",
                "position": 0,
                "thumbnail_url": None,
            }
        ]

    return []


@router.get("/search")
def search_creator_content(
    q: str = Query(
        ...,
        min_length=1,
        max_length=200,
        description="Search title, caption or hashtag",
    ),
    content_type: Literal["video", "reel", "post"] = Query(
        ...,
        description="Selected Content tab",
    ),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Search only the authenticated creator's content, newest first."""

    term = q.strip().lstrip("#").strip()

    if not term:
        return {
            "content_type": content_type,
            "query": q,
            "page": page,
            "limit": limit,
            "total": 0,
            "items": [],
        }

    pattern = f"%{term}%"
    offset = (page - 1) * limit

    # ---------------------------------------------------------
    # POSTS: image-only posts, searched by caption or hashtag
    # ---------------------------------------------------------
    if content_type == "post":

        has_gallery_media = exists().where(
            models.PostMedia.post_id == models.Post.id
        )

        image_in_gallery = exists().where(
            models.PostMedia.post_id == models.Post.id,
            models.PostMedia.media_type == models.MediaType.image,
        )

        legacy_image_without_gallery = and_(
            models.Post.media_type == models.MediaType.image,
            ~has_gallery_media,
        )

        hashtag_match = exists().where(
            models.PostHashtag.post_id == models.Post.id,
            models.PostHashtag.hashtag_id == models.Hashtag.id,
            models.Hashtag.name.ilike(term),
        )

        query = db.query(models.Post).filter(
            models.Post.user_id == current_user.id,
            or_(
                image_in_gallery,
                legacy_image_without_gallery,
            ),
            or_(
                models.Post.caption.ilike(pattern),
                hashtag_match,
            ),
        )

        total = query.count()

        rows = (
            query
            .order_by(
                models.Post.created_at.desc(),
                models.Post.id.desc(),
            )
            .offset(offset)
            .limit(limit)
            .all()
        )

        items = []

        for post in rows:
            media = _image_media(post)

            if not media:
                continue

            items.append({
                "id": post.id,
                "content_type": "post",
                "title": None,
                "caption": post.caption,
                "thumbnail_url": media[0]["thumbnail_url"] or media[0]["media_url"],
                "media": media,
                "views": None,
                "created_at": (
                    post.created_at.isoformat()
                    if post.created_at
                    else None
                ),
            })

        return {
            "content_type": content_type,
            "query": q,
            "page": page,
            "limit": limit,
            "total": total,
            "items": items,
        }

    # ---------------------------------------------------------
    # VIDEOS / REELS: currently share the same Reel table
    # ---------------------------------------------------------
    query = db.query(models.Reel).filter(
        models.Reel.user_id == current_user.id,
        or_(
            models.Reel.title.ilike(pattern),
            models.Reel.caption.ilike(pattern),
        ),
    )

    total = query.count()

    rows = (
        query
        .order_by(
            models.Reel.created_at.desc(),
            models.Reel.id.desc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )

    ids = [row.id for row in rows]
    view_counts = {}

    if ids:
        counts = (
            db.query(
                models.ReelInsightEvent.reel_id,
                func.count(models.ReelInsightEvent.id),
            )
            .filter(
                models.ReelInsightEvent.reel_id.in_(ids),
                models.ReelInsightEvent.event_type == "view",
            )
            .group_by(models.ReelInsightEvent.reel_id)
            .all()
        )

        view_counts = {
            reel_id: int(count)
            for reel_id, count in counts
        }

    items = [
        {
            "id": row.id,
            "content_type": content_type,
            "title": row.title,
            "caption": row.caption,
            "thumbnail_url": row.thumbnail_url,
            "media_url": row.video_url,
            "views": view_counts.get(row.id, 0),
            "created_at": (
                row.created_at.isoformat()
                if row.created_at
                else None
            ),
        }
        for row in rows
    ]

    return {
        "content_type": content_type,
        "query": q,
        "page": page,
        "limit": limit,
        "total": total,
        "items": items,
    }
