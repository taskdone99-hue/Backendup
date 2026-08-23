"""
Posts and Reels: creation, CRUD, feeds, and reel-audio remixing.

Comments and likes for these live in comment_routes.py; the "video upload /
thumbnail / metadata / collaborators / revenue-split" flow lives in
video_routes.py (it operates on the same Reel rows created here, since this
app has a single video-content type).
"""

import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user, get_current_user_optional
from app.services.media_service import delete_media_file, generate_video_thumbnail, save_upload_file
from app.services import engagement

router = APIRouter(prefix="/api/posts", tags=["posts"])
reels_router = APIRouter(prefix="/api/reels", tags=["reels"])

_HASHTAG_PATTERN = re.compile(r"#(\w+)")


def _extract_hashtags(caption: str | None) -> list[str]:
    """Pulls #tags out of a caption, de-duplicated, in first-seen order.

    Captions are free text (there's no separate hashtags table), so this
    is computed on read rather than stored — good enough for display, but
    not queryable/filterable in the DB. If hashtag search/discovery is
    ever needed, promote this into a real PostHashtag table populated on
    post create/update instead.
    """
    if not caption:
        return []
    seen: dict[str, None] = {}
    for match in _HASHTAG_PATTERN.finditer(caption):
        tag = match.group(1)
        seen.setdefault(tag, None)
    return list(seen.keys())


# ---- internal helpers ----

def _to_post_detail(
    db: Session, post: models.Post, viewer_id: int | None
) -> schemas.PostDetailOut:
    detail = schemas.PostDetailOut.model_validate(post)
    detail.author = schemas.UserSummaryOut.model_validate(post.user)
    detail.likes_count = engagement.likes_count(db, models.LikeTargetType.post, post.id)
    detail.comments_count = engagement.comments_count(db, post.id)
    detail.share_count = engagement.shares_count(db, models.ShareContentType.post, post.id)
    detail.hashtags = _extract_hashtags(post.caption)
    detail.like_id = engagement.get_like_id(
        db, viewer_id, models.LikeTargetType.post, post.id
    )
    detail.is_liked = detail.like_id is not None
    detail.is_saved = engagement.is_saved_by(
        db, viewer_id, post.id, models.SavedItemType.post
    )
    if post.music_url:
        detail.music = schemas.MusicOut(
            title=post.music_title,
            artist=post.music_artist,
            audio_url=post.music_url,
            start_seconds=post.music_start_seconds or 0,
        )
    if post.location_name:
        detail.location = schemas.LocationOut(
            name=post.location_name,
            latitude=post.location_latitude,
            longitude=post.location_longitude,
        )
    detail.tags_count = (
        db.query(models.PostTag).filter(models.PostTag.post_id == post.id).count()
    )
    detail.members_count = (
        db.query(models.PostMember).filter(models.PostMember.post_id == post.id).count()
    )
    detail.tags = [
        schemas.UserSummaryOut.model_validate(t.user)
        for t in db.query(models.PostTag).filter(models.PostTag.post_id == post.id).all()
    ]
    detail.members = [
        schemas.UserSummaryOut.model_validate(m.user)
        for m in db.query(models.PostMember).filter(models.PostMember.post_id == post.id).all()
    ]
    return detail


def _replace_post_tags(db: Session, post: models.Post, user_ids: list[int]) -> None:
    """Full replace: tags the given users, untags anyone left off the list."""
    user_ids = list(dict.fromkeys(user_ids))  # de-dupe, keep order
    if user_ids:
        found = db.query(models.User.id).filter(models.User.id.in_(user_ids)).all()
        found_ids = {row[0] for row in found}
        missing = set(user_ids) - found_ids
        if missing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User id(s) not found: {sorted(missing)}",
            )

    existing = {
        t.user_id: t
        for t in db.query(models.PostTag).filter(models.PostTag.post_id == post.id).all()
    }
    for uid in set(existing) - set(user_ids):
        db.delete(existing[uid])
    for uid in user_ids:
        if uid not in existing:
            db.add(models.PostTag(post_id=post.id, user_id=uid))


def _replace_post_members(db: Session, post: models.Post, user_ids: list[int]) -> None:
    """Full replace of the post's collaborators/members list."""
    user_ids = list(dict.fromkeys(user_ids))
    if user_ids:
        found = db.query(models.User.id).filter(models.User.id.in_(user_ids)).all()
        found_ids = {row[0] for row in found}
        missing = set(user_ids) - found_ids
        if missing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User id(s) not found: {sorted(missing)}",
            )

    existing = {
        m.user_id: m
        for m in db.query(models.PostMember).filter(models.PostMember.post_id == post.id).all()
    }
    for uid in set(existing) - set(user_ids):
        db.delete(existing[uid])
    for uid in user_ids:
        if uid not in existing:
            db.add(models.PostMember(post_id=post.id, user_id=uid))


def _to_reel_detail(
    db: Session, reel: models.Reel, viewer_id: int | None
) -> schemas.ReelDetailOut:
    detail = schemas.ReelDetailOut.model_validate(reel)
    detail.author = schemas.UserSummaryOut.model_validate(reel.user)
    detail.likes_count = engagement.likes_count(db, models.LikeTargetType.reel, reel.id)
    detail.like_id = engagement.get_like_id(db, viewer_id, models.LikeTargetType.reel, reel.id)
    detail.is_liked = detail.like_id is not None
    detail.is_saved = engagement.is_saved_by(
        db, viewer_id, reel.id, models.SavedItemType.reel
    )
    return detail


def _get_post_or_404(db: Session, post_id: int) -> models.Post:
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if post is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    return post


def _get_reel_or_404(db: Session, reel_id: int) -> models.Reel:
    reel = db.query(models.Reel).filter(models.Reel.id == reel_id).first()
    if reel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reel not found")
    return reel


def _following_ids(db: Session, user_id: int) -> list[int]:
    return [
        row[0]
        for row in db.query(models.Follow.following_id)
        .filter(models.Follow.follower_id == user_id)
        .all()
    ]


# ==========================================================================
# Posts
# ==========================================================================

@router.post("", response_model=schemas.PostDetailOut, status_code=status.HTTP_201_CREATED)
def create_post(
    file: UploadFile,
    caption: str | None = Form(default=None),
    alt_text: str | None = Form(default=None),
    ai_generated: bool = Form(default=False),
    music_title: str | None = Form(default=None),
    music_artist: str | None = Form(default=None),
    music_url: str | None = Form(default=None),
    music_start_seconds: int = Form(default=0),
    location_name: str | None = Form(default=None),
    location_latitude: float | None = Form(default=None),
    location_longitude: float | None = Form(default=None),
    tag_user_ids: str | None = Form(
        default=None, description="Comma-separated user ids, e.g. '12,15,20'"
    ),
    member_user_ids: str | None = Form(
        default=None, description="Comma-separated user ids, e.g. '12,15,20'"
    ),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Everything a post can carry is settable at creation time now — music,
    location, alt_text, ai_generated, tags, and collaborators/members — so
    a client doesn't need a create-then-PUT round trip. All of these stay
    optional and settable later too, either via PUT /api/posts/{id} (JSON)
    or the individual post-details endpoints (/tags, /music, /location,
    etc) if you'd rather set them one at a time or after the fact.
    Hashtags aren't a separate field — they're parsed out of `caption`
    automatically (see hashtags in the response).
    tag_user_ids / member_user_ids are multipart form fields, so they're
    plain comma-separated strings here rather than JSON arrays (multipart
    can't carry nested types) — e.g. "12,15,20".
    """
    def _parse_ids(raw: str | None, field_name: str) -> list[int]:
        if not raw or not raw.strip():
            return []
        try:
            return [int(x.strip()) for x in raw.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{field_name} must be comma-separated integers, e.g. '12,15,20'",
            )

    tag_ids = _parse_ids(tag_user_ids, "tag_user_ids")
    member_ids = _parse_ids(member_user_ids, "member_user_ids")

    url, kind = save_upload_file(file, "posts", allow_video=True)
    post = models.Post(
        user_id=current_user.id,
        caption=caption,
        media_url=url,
        media_type=models.MediaType.video if kind == "video" else models.MediaType.image,
        alt_text=alt_text,
        ai_generated=ai_generated,
        music_title=music_title,
        music_artist=music_artist,
        music_url=music_url,
        music_start_seconds=music_start_seconds if music_title or music_url else None,
        location_name=location_name,
        location_latitude=location_latitude,
        location_longitude=location_longitude,
    )
    db.add(post)
    db.flush()

    if tag_ids:
        _replace_post_tags(db, post, tag_ids)
    if member_ids:
        _replace_post_members(db, post, member_ids)

    db.commit()
    db.refresh(post)
    return _to_post_detail(db, post, current_user.id)


# NOTE: /feed and /explore must be registered before the /{post_id} routes
# below — Starlette matches routes in registration order, so a literal path
# declared after a "/{post_id}" pattern would never be reached.

@router.get("/feed", response_model=schemas.PaginatedPostDetailResponse)
def get_home_feed(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Posts from people the current user follows, plus their own, newest first."""
    author_ids = _following_ids(db, current_user.id) + [current_user.id]
    query = db.query(models.Post).filter(models.Post.user_id.in_(author_ids))
    total = query.count()
    posts = query.order_by(models.Post.created_at.desc()).offset(offset).limit(limit).all()
    items = [_to_post_detail(db, p, current_user.id) for p in posts]
    return schemas.PaginatedPostDetailResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/explore", response_model=schemas.PaginatedPostDetailResponse)
def get_explore_feed(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    """Posts from accounts the current user doesn't already follow (all posts, if logged out)."""
    query = db.query(models.Post)
    if current_user is not None:
        excluded_ids = _following_ids(db, current_user.id) + [current_user.id]
        query = query.filter(models.Post.user_id.notin_(excluded_ids))
    total = query.count()
    posts = query.order_by(models.Post.created_at.desc()).offset(offset).limit(limit).all()
    viewer_id = current_user.id if current_user else None
    items = [_to_post_detail(db, p, viewer_id) for p in posts]
    return schemas.PaginatedPostDetailResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/{post_id}", response_model=schemas.PostDetailOut)
def get_post(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    post = _get_post_or_404(db, post_id)
    return _to_post_detail(db, post, current_user.id if current_user else None)


@router.put("/{post_id}", response_model=schemas.PostDetailOut)
def update_post(
    post_id: int,
    payload: schemas.PostUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = _get_post_or_404(db, post_id)
    if post.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only update your own posts"
        )

    updates = payload.model_dump(exclude_unset=True)

    if "caption" in updates:
        post.caption = updates["caption"]

    if "alt_text" in updates:
        post.alt_text = updates["alt_text"]

    if "ai_generated" in updates and updates["ai_generated"] is not None:
        post.ai_generated = updates["ai_generated"]

    if "music" in updates:
        music = updates["music"]
        if music is None:
            post.music_title = None
            post.music_artist = None
            post.music_url = None
            post.music_start_seconds = None
        else:
            post.music_title = music["title"]
            post.music_artist = music.get("artist")
            post.music_url = music["audio_url"]
            post.music_start_seconds = music.get("start_seconds", 0)

    if "location" in updates:
        location = updates["location"]
        if location is None:
            post.location_name = None
            post.location_latitude = None
            post.location_longitude = None
        else:
            post.location_name = location["name"]
            post.location_latitude = location.get("latitude")
            post.location_longitude = location.get("longitude")

    if updates.get("tag_user_ids") is not None:
        _replace_post_tags(db, post, updates["tag_user_ids"])

    if updates.get("member_user_ids") is not None:
        _replace_post_members(db, post, updates["member_user_ids"])

    db.commit()
    db.refresh(post)
    return _to_post_detail(db, post, current_user.id)


@router.put("/{post_id}/media", response_model=schemas.PostDetailOut)
def update_post_media(
    post_id: int,
    file: UploadFile,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Replace a post's image/video. Separate from PUT /:id because this
    needs multipart, not JSON."""
    post = _get_post_or_404(db, post_id)
    if post.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only update your own posts"
        )

    old_url = post.media_url
    url, kind = save_upload_file(file, "posts", allow_video=True)
    post.media_url = url
    post.media_type = models.MediaType.video if kind == "video" else models.MediaType.image
    db.commit()
    db.refresh(post)
    delete_media_file(old_url)
    return _to_post_detail(db, post, current_user.id)


@router.delete("/{post_id}", response_model=schemas.MessageResponse)
def delete_post(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = _get_post_or_404(db, post_id)
    if post.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only delete your own posts"
        )

    # Comments and likes reference this post by plain id (not a SQLAlchemy
    # ForeignKey/relationship — see models.Comment / models.Like), so the
    # ORM can't cascade-delete them automatically the way it does for
    # `saves` below. Clean them up explicitly.
    comment_ids = [
        row[0]
        for row in db.query(models.Comment.id).filter(models.Comment.post_id == post_id).all()
    ]
    if comment_ids:
        db.query(models.Like).filter(
            models.Like.target_type == models.LikeTargetType.comment,
            models.Like.target_id.in_(comment_ids),
        ).delete(synchronize_session=False)
        db.query(models.Comment).filter(models.Comment.post_id == post_id).delete(
            synchronize_session=False
        )

    db.query(models.Like).filter(
        models.Like.target_type == models.LikeTargetType.post,
        models.Like.target_id == post_id,
    ).delete(synchronize_session=False)

    media_url = post.media_url
    db.delete(post)  # cascades saved_posts via the ORM relationship
    db.commit()
    delete_media_file(media_url)
    return schemas.MessageResponse(message="Post deleted")


def _save_target(
    db: Session, user_id: int, target_type: models.SavedItemType, target_id: int
) -> None:
    existing = (
        db.query(models.SavedItem)
        .filter(
            models.SavedItem.user_id == user_id,
            models.SavedItem.target_type == target_type,
            models.SavedItem.target_id == target_id,
        )
        .first()
    )
    if existing is None:
        db.add(
            models.SavedItem(user_id=user_id, target_type=target_type, target_id=target_id)
        )
        db.commit()


def _unsave_target(
    db: Session, user_id: int, target_type: models.SavedItemType, target_id: int
) -> None:
    existing = (
        db.query(models.SavedItem)
        .filter(
            models.SavedItem.user_id == user_id,
            models.SavedItem.target_type == target_type,
            models.SavedItem.target_id == target_id,
        )
        .first()
    )
    if existing is not None:
        db.delete(existing)
        db.commit()


@router.post("/{post_id}/save", response_model=schemas.MessageResponse)
def save_post(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _get_post_or_404(db, post_id)
    _save_target(db, current_user.id, models.SavedItemType.post, post_id)
    return schemas.MessageResponse(message="Post saved")


@router.delete("/{post_id}/save", response_model=schemas.MessageResponse)
def unsave_post(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _unsave_target(db, current_user.id, models.SavedItemType.post, post_id)
    return schemas.MessageResponse(message="Post unsaved")


# ==========================================================================
# Reels
# ==========================================================================

@reels_router.post("", response_model=schemas.ReelDetailOut, status_code=status.HTTP_201_CREATED)
def create_reel(
    file: UploadFile,
    caption: str | None = Form(default=None),
    thumbnail: UploadFile | None = File(default=None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    `thumbnail` is optional — send a poster-frame image alongside the video
    in the same multipart request and it's used as thumbnail_url as-is.
    If you don't send one, the backend automatically extracts a frame from
    the uploaded video itself (via ffmpeg) and uses that instead — you
    don't need to generate or upload a thumbnail on the frontend at all
    unless you want to override the auto-generated one. If ffmpeg isn't
    available on the server or the video can't be read, thumbnail_url
    just comes back null (never blocks the reel from being created); you
    can still call POST /api/videos/{id}/thumbnail afterward to attach one.
    """
    url, kind = save_upload_file(file, "reels", allow_video=True)
    if kind != "video":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Reels must be a video file"
        )

    thumbnail_url = None
    if thumbnail is not None and thumbnail.filename:
        thumbnail_url, thumb_kind = save_upload_file(thumbnail, "thumbnails", allow_video=False)
        if thumb_kind != "image":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Thumbnail must be an image file"
            )
    else:
        thumbnail_url = generate_video_thumbnail(url)

    reel = models.Reel(
        user_id=current_user.id, caption=caption, video_url=url, thumbnail_url=thumbnail_url
    )
    db.add(reel)
    db.commit()
    db.refresh(reel)
    return _to_reel_detail(db, reel, current_user.id)


# NOTE: /feed and /trending must stay registered before /{reel_id} for the
# same route-ordering reason as posts above.

@reels_router.get("/home", response_model=schemas.PaginatedReelDetailResponse)
def get_reels_home_feed(
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Reels from people the current user follows, plus their own — the
    reel-equivalent of GET /api/posts/feed, for surfacing a user's reel on
    the Home page. GET /api/reels/feed (below) stays as the global,
    Explore-style reel feed — this is the follows-scoped one.
    """
    author_ids = _following_ids(db, current_user.id) + [current_user.id]
    query = db.query(models.Reel).filter(models.Reel.user_id.in_(author_ids))
    total = query.count()
    reels = query.order_by(models.Reel.created_at.desc()).offset(offset).limit(limit).all()
    items = [_to_reel_detail(db, r, current_user.id) for r in reels]
    return schemas.PaginatedReelDetailResponse(total=total, limit=limit, offset=offset, items=items)


@reels_router.get("/feed", response_model=schemas.PaginatedReelDetailResponse)
def get_reels_feed(
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    """
    Infinite-scroll reel feed. Reverse-chronological with limit/offset —
    the same pagination style used everywhere else in this API — so the
    client advances `offset` by `limit` each time it scrolls to the next page.
    """
    query = db.query(models.Reel)
    total = query.count()
    reels = query.order_by(models.Reel.created_at.desc()).offset(offset).limit(limit).all()
    viewer_id = current_user.id if current_user else None
    items = [_to_reel_detail(db, r, viewer_id) for r in reels]
    return schemas.PaginatedReelDetailResponse(total=total, limit=limit, offset=offset, items=items)


@reels_router.get("/trending", response_model=schemas.PaginatedReelDetailResponse)
def get_trending_reels(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    days: int = Query(7, ge=1, le=90, description="Trending window, in days"),
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    """
    Ranked by an engagement score (likes + valid watch sessions) within the
    trailing `days` window, rather than raw like count alone — a reel that
    gets watched heavily but isn't liked much can still surface, and vice versa.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    likes_subq = (
        db.query(
            models.Like.target_id.label("reel_id"),
            func.count(models.Like.id).label("like_score"),
        )
        .filter(
            models.Like.target_type == models.LikeTargetType.reel,
            models.Like.created_at >= since,
        )
        .group_by(models.Like.target_id)
        .subquery()
    )
    watch_subq = (
        db.query(
            models.WatchSession.reel_id.label("reel_id"),
            func.count(models.WatchSession.id).label("watch_score"),
        )
        .filter(
            models.WatchSession.is_valid.is_(True),
            models.WatchSession.started_at >= since,
        )
        .group_by(models.WatchSession.reel_id)
        .subquery()
    )

    score = func.coalesce(likes_subq.c.like_score, 0) + func.coalesce(watch_subq.c.watch_score, 0)

    query = (
        db.query(models.Reel, score.label("score"))
        .outerjoin(likes_subq, models.Reel.id == likes_subq.c.reel_id)
        .outerjoin(watch_subq, models.Reel.id == watch_subq.c.reel_id)
        .filter(score > 0)
        .order_by(score.desc(), models.Reel.created_at.desc())
    )

    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    viewer_id = current_user.id if current_user else None
    items = [_to_reel_detail(db, reel, viewer_id) for reel, _score in rows]
    return schemas.PaginatedReelDetailResponse(total=total, limit=limit, offset=offset, items=items)


@reels_router.get("/{reel_id}", response_model=schemas.ReelDetailOut)
def get_reel(
    reel_id: int,
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    reel = _get_reel_or_404(db, reel_id)
    return _to_reel_detail(db, reel, current_user.id if current_user else None)


@reels_router.delete("/{reel_id}", response_model=schemas.MessageResponse)
def delete_reel(
    reel_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    reel = _get_reel_or_404(db, reel_id)
    if reel.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only delete your own reels"
        )

    db.query(models.Like).filter(
        models.Like.target_type == models.LikeTargetType.reel,
        models.Like.target_id == reel_id,
    ).delete(synchronize_session=False)

    # WatchSession.reel_id and SeriesReel.reel_id are real foreign keys
    # with no ON DELETE CASCADE — deleting a reel that anyone has ever
    # watched (very likely, since watch tracking is used throughout the
    # app) or that's part of a Series would otherwise hit a foreign key
    # violation on MySQL/InnoDB and 500. Clear them explicitly first, the
    # same way Like is handled above.
    db.query(models.WatchSession).filter(models.WatchSession.reel_id == reel_id).delete(
        synchronize_session=False
    )
    db.query(models.SeriesReel).filter(models.SeriesReel.reel_id == reel_id).delete(
        synchronize_session=False
    )
    # Audio.source_reel_id is nullable and only an optional "traced back
    # to" reference — null it out rather than deleting the Audio row
    # itself, since a bookmarked sound should survive its source reel
    # being removed.
    db.query(models.Audio).filter(models.Audio.source_reel_id == reel_id).update(
        {models.Audio.source_reel_id: None}, synchronize_session=False
    )

    video_url, thumbnail_url = reel.video_url, reel.thumbnail_url
    db.delete(reel)  # cascades collaborators + revenue splits via the ORM relationship
    db.commit()
    delete_media_file(video_url)
    if thumbnail_url:
        delete_media_file(thumbnail_url)
    return schemas.MessageResponse(message="Reel deleted")


@reels_router.post(
    "/{reel_id}/audio-remix",
    response_model=schemas.ReelDetailOut,
    status_code=status.HTTP_201_CREATED,
)
def remix_reel_audio(
    reel_id: int,
    file: UploadFile,
    caption: str | None = Form(default=None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Creates a new reel that reuses an existing reel's audio — new visuals
    (the uploaded file), paired with the original via `remixed_from_id`,
    the same shape as a "use this audio" remix.
    """
    original = _get_reel_or_404(db, reel_id)

    url, kind = save_upload_file(file, "reels", allow_video=True)
    if kind != "video":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Reels must be a video file"
        )

    remix = models.Reel(
        user_id=current_user.id,
        caption=caption,
        video_url=url,
        remixed_from_id=original.id,
    )
    db.add(remix)
    db.commit()
    db.refresh(remix)
    return _to_reel_detail(db, remix, current_user.id)


@reels_router.post("/{reel_id}/save", response_model=schemas.MessageResponse)
def save_reel(
    reel_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _get_reel_or_404(db, reel_id)
    _save_target(db, current_user.id, models.SavedItemType.reel, reel_id)
    return schemas.MessageResponse(message="Reel saved")


@reels_router.delete("/{reel_id}/save", response_model=schemas.MessageResponse)
def unsave_reel(
    reel_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _unsave_target(db, current_user.id, models.SavedItemType.reel, reel_id)
    return schemas.MessageResponse(message="Reel unsaved")
