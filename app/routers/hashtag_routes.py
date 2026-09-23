"""
Hashtag discovery: GET /api/hashtags/{name}, /{name}/posts, and /trending.

Hashtags themselves are never created directly by a client — they're
parsed out of post captions on create/update (see
app/services/hashtag_service.py, called from content_routes.py) — so this
router is read-only.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user, get_current_user_optional
from app.routers.content_routes import _to_post_detail, _visible_authors_clause
from app.services.hashtag_service import hashtag_posts_count

router = APIRouter(prefix="/api/hashtags", tags=["hashtags"])


def _normalize(name: str) -> str:
    return name.strip().lstrip("#").lower()


def _get_hashtag_or_404(db: Session, name: str) -> models.Hashtag:
    hashtag = db.query(models.Hashtag).filter(models.Hashtag.name == _normalize(name)).first()
    if hashtag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hashtag not found")
    return hashtag


def _get_or_create_hashtag(db: Session, name: str) -> models.Hashtag:
    """Following a hashtag is valid even if nobody has posted under it yet
    (Instagram lets you follow any hashtag by name) — unlike every other
    lookup in this file, which 404s because there's nothing to *show* for
    a hashtag with zero posts."""
    normalized = _normalize(name)
    hashtag = db.query(models.Hashtag).filter(models.Hashtag.name == normalized).first()
    if hashtag is None:
        hashtag = models.Hashtag(name=normalized)
        db.add(hashtag)
        db.commit()
        db.refresh(hashtag)
    return hashtag


# NOTE: /trending must be registered before /{name} — Starlette matches
# routes in registration order, so a literal path declared after a
# "/{name}" pattern would never be reached (same reason /api/posts/feed
# and /explore are registered before /api/posts/{post_id}).

# NOTE: /trending and /following must be registered before /{name} —
# Starlette matches routes in registration order, so a literal path
# declared after a "/{name}" pattern would never be reached (same reason
# /api/posts/feed and /explore are registered before /api/posts/{post_id}).

@router.get("/following", response_model=schemas.PaginatedHashtagsResponse)
def get_followed_hashtags(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    query = (
        db.query(models.Hashtag)
        .join(models.HashtagFollow, models.HashtagFollow.hashtag_id == models.Hashtag.id)
        .filter(models.HashtagFollow.user_id == current_user.id)
    )
    total = query.count()
    rows = (
        query.order_by(models.HashtagFollow.created_at.desc()).offset(offset).limit(limit).all()
    )
    items = [
        schemas.HashtagOut(name=h.name, posts_count=hashtag_posts_count(db, h)) for h in rows
    ]
    return schemas.PaginatedHashtagsResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/trending", response_model=schemas.PaginatedTrendingHashtagsResponse)
def get_trending_hashtags(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    days: int = Query(7, ge=1, le=90, description="Trending window, in days"),
    db: Session = Depends(get_db),
):
    """Hashtags ranked by how many posts used them in the trailing `days`
    window (ties broken by all-time post count). Only counts posts made in
    that window — an old, dormant hashtag with a huge historical total
    won't outrank one that's actively being used right now."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    recent_subq = (
        db.query(
            models.PostHashtag.hashtag_id.label("hashtag_id"),
            func.count(models.PostHashtag.id).label("recent_count"),
        )
        .filter(models.PostHashtag.created_at >= since)
        .group_by(models.PostHashtag.hashtag_id)
        .subquery()
    )
    total_subq = (
        db.query(
            models.PostHashtag.hashtag_id.label("hashtag_id"),
            func.count(models.PostHashtag.id).label("total_count"),
        )
        .group_by(models.PostHashtag.hashtag_id)
        .subquery()
    )

    query = (
        db.query(
            models.Hashtag,
            recent_subq.c.recent_count,
            func.coalesce(total_subq.c.total_count, 0).label("total_count"),
        )
        .join(recent_subq, models.Hashtag.id == recent_subq.c.hashtag_id)
        .outerjoin(total_subq, models.Hashtag.id == total_subq.c.hashtag_id)
        .order_by(recent_subq.c.recent_count.desc(), models.Hashtag.name.asc())
    )

    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    items = [
        schemas.TrendingHashtagOut(
            name=hashtag.name, posts_count=total_count, recent_posts_count=recent_count
        )
        for hashtag, recent_count, total_count in rows
    ]
    return schemas.PaginatedTrendingHashtagsResponse(
        total=total, limit=limit, offset=offset, items=items
    )


@router.get("/{name}", response_model=schemas.HashtagOut)
def get_hashtag(name: str, db: Session = Depends(get_db)):
    hashtag = _get_hashtag_or_404(db, name)
    return schemas.HashtagOut(name=hashtag.name, posts_count=hashtag_posts_count(db, hashtag))


@router.get("/{name}/posts", response_model=schemas.PaginatedPostDetailResponse)
def get_hashtag_posts(
    name: str,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    """Posts carrying this hashtag, newest first — same private-account
    visibility rule as the explore feed (see _visible_authors_clause)."""
    hashtag = _get_hashtag_or_404(db, name)
    viewer_id = current_user.id if current_user else None

    query = (
        db.query(models.Post)
        .join(models.PostHashtag, models.PostHashtag.post_id == models.Post.id)
        .join(models.User, models.Post.user_id == models.User.id)
        .filter(models.PostHashtag.hashtag_id == hashtag.id)
        .filter(_visible_authors_clause(db, viewer_id))
    )
    total = query.count()
    posts = query.order_by(models.Post.created_at.desc()).offset(offset).limit(limit).all()
    items = [_to_post_detail(db, p, viewer_id) for p in posts]
    return schemas.PaginatedPostDetailResponse(total=total, limit=limit, offset=offset, items=items)


@router.post("/{name}/follow", response_model=schemas.HashtagFollowActionResponse)
def follow_hashtag(
    name: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    hashtag = _get_or_create_hashtag(db, name)

    existing = (
        db.query(models.HashtagFollow)
        .filter(
            models.HashtagFollow.user_id == current_user.id,
            models.HashtagFollow.hashtag_id == hashtag.id,
        )
        .first()
    )
    if existing is None:
        db.add(models.HashtagFollow(user_id=current_user.id, hashtag_id=hashtag.id))
        db.commit()

    return schemas.HashtagFollowActionResponse(
        message=f"Following #{hashtag.name}", is_following=True
    )


@router.delete("/{name}/follow", response_model=schemas.HashtagFollowActionResponse)
def unfollow_hashtag(
    name: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    hashtag = _get_hashtag_or_404(db, name)

    existing = (
        db.query(models.HashtagFollow)
        .filter(
            models.HashtagFollow.user_id == current_user.id,
            models.HashtagFollow.hashtag_id == hashtag.id,
        )
        .first()
    )
    if existing is not None:
        db.delete(existing)
        db.commit()

    return schemas.HashtagFollowActionResponse(
        message=f"Unfollowed #{hashtag.name}", is_following=False
    )
