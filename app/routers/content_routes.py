"""
Posts and Reels: creation, CRUD, feeds, and reel-audio remixing.

Comments and likes for these live in comment_routes.py; the "video upload /
thumbnail / metadata / collaborators / revenue-split" flow lives in
video_routes.py (it operates on the same Reel rows created here, since this
app has a rom asingle video-content type).
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user, get_current_user_optional
from app.form_fields import OptionalFloatForm, OptionalIntForm
from app.services.media_service import (
    delete_media_file,
    generate_video_thumbnail,
    get_video_duration,
    save_upload_file,
)
from app.services import engagement
from app.services.location_service import resolve_location_from_form, find_or_create_location
from app.services.hashtag_service import extract_hashtags, sync_post_hashtags
from app.services.privacy_service import blocked_user_ids, muted_user_ids, is_blocked

router = APIRouter(prefix="/api/posts", tags=["posts"])
reels_router = APIRouter(prefix="/api/reels", tags=["reels"])


# ---- internal helpers ----

def _to_post_detail(
    db: Session, post: models.Post, viewer_id: int | None
) -> schemas.PostDetailOut:
    detail = schemas.PostDetailOut.model_validate(post)
    detail.user = schemas.UserSummaryOut.model_validate(post.user)
    detail.likes_count = engagement.likes_count(db, models.LikeTargetType.post, post.id)
    detail.comments_count = engagement.comments_count(db, post.id)
    detail.share_count = engagement.shares_count(db, models.ShareContentType.post, post.id)
    detail.hashtags = extract_hashtags(post.caption)
    if post.media_items:
        detail.media = [
            schemas.MediaItemOut.model_validate(m) for m in post.media_items
        ]
    else:
        # Legacy post created before the post_media table existed — synthesize
        # a single-item list from the flat columns so `media` is never empty.
        detail.media = [
            schemas.MediaItemOut(
                id=post.id, media_url=post.media_url, media_type=post.media_type, position=0,
                caption=post.caption,
            )
        ]
    detail.media_count = len(detail.media)
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
    if post.location_id and post.location is not None:
        detail.location = schemas.LocationOut.model_validate(post.location)
    elif post.location_name:
        # Pre-Location-table posts (or ones only given flat fields) —
        # same fallback shape as before, just with the new optional fields
        # left null.
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
    detail.user = schemas.UserSummaryOut.model_validate(reel.user)
    detail.likes_count = engagement.likes_count(db, models.LikeTargetType.reel, reel.id)
    detail.like_id = engagement.get_like_id(db, viewer_id, models.LikeTargetType.reel, reel.id)
    detail.is_liked = detail.like_id is not None
    detail.comments_count = engagement.comments_count(db, reel_id=reel.id)
    detail.is_saved = engagement.is_saved_by(
        db, viewer_id, reel.id, models.SavedItemType.reel
    )
    if reel.location_id and reel.location is not None:
        detail.location = schemas.LocationOut.model_validate(reel.location)
    elif reel.location_name:
        detail.location = schemas.LocationOut(
            name=reel.location_name,
            latitude=reel.location_latitude,
            longitude=reel.location_longitude,
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


def _visible_authors_clause(db: Session, viewer_id: int | None):
    """
    Filter clause for discovery-style feeds (explore, global reels feed,
    trending) that queries across all accounts rather than a specific
    profile: excludes content from private accounts unless the viewer is
    the author themselves or already follows them, AND excludes content
    from anyone blocked in either direction (see
    app/services/privacy_service.blocked_user_ids) — block is a full,
    symmetric exclusion, unlike mute, which only touches follows-scoped
    feeds (see get_home_feed / get_reels_home_feed below). Requires the
    query to be joined to models.User on the content's user_id first, e.g.:
        query.join(models.User, models.Post.user_id == models.User.id)
             .filter(_visible_authors_clause(db, viewer_id))
    """
    if viewer_id is None:
        return models.User.is_private == False
    base = or_(
        models.User.is_private == False,
        models.User.id == viewer_id,
        models.User.id.in_(_following_ids(db, viewer_id)),
    )
    blocked_ids = blocked_user_ids(db, viewer_id)
    if blocked_ids:
        return and_(base, models.User.id.notin_(blocked_ids))
    return base


def _require_author_visible(db: Session, author: models.User, viewer_id: int | None) -> None:
    """Same rule as _visible_authors_clause, but for a single already-fetched
    item (a specific post/reel by id) rather than a list query. A block
    (either direction) hides the item entirely — 404, not 403, same as if
    it never existed for this viewer — checked before the private-account
    rule below since it's the stricter case."""
    if viewer_id is not None and viewer_id != author.id and is_blocked(db, viewer_id, author.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not author.is_private:
        return
    if viewer_id is not None and (
        viewer_id == author.id or author.id in _following_ids(db, viewer_id)
    ):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account is private")


# ==========================================================================
# Posts
# ==========================================================================

@router.post("", response_model=schemas.PostDetailOut, status_code=status.HTTP_201_CREATED)
def create_post(
    file: UploadFile | None = File(
        default=None,
        description="Single-photo/video post. Keep using this field name for "
        "existing single-upload clients — unchanged from before.",
    ),
    files: list[UploadFile] | None = File(
        default=None,
        description="Carousel post: send 2+ files under this field name "
        "instead of `file` to attach multiple photos/videos to one post. "
        "The response's `media` array lists all of them, in upload order.",
    ),
    caption: str | None = Form(default=None),
    media_captions: list[str] | None = Form(default=None, description="One caption per uploaded file, in the same order; trailing captions may be omitted."),
    alt_text: str | None = Form(default=None),
    ai_generated: bool = Form(default=False),
    music_title: str | None = Form(default=None),
    music_artist: str | None = Form(default=None),
    music_url: str | None = Form(default=None),
    music_start_seconds: int = Form(default=0),
    location_name: str | None = Form(default=None),
    location_latitude: float | None = Form(default=None),
    location_longitude: float | None = Form(default=None),
    location_id: int | None = Form(
        default=None, description="Attach an already-saved location (see POST /api/locations) by id"
    ),
    location_address: str | None = Form(default=None),
    location_city: str | None = Form(default=None),
    location_state: str | None = Form(default=None),
    location_country: str | None = Form(default=None),
    location_place_id: str | None = Form(default=None),
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
    automatically (see hashtags in the response), and persisted so they're
    browsable via GET /api/hashtags/{name}/posts and /api/hashtags/trending.
    For media, send one file under `file` (unchanged, single-photo/video
    post) or two-or-more under `files` (carousel post) — not both. The
    response's `media` list has one entry per attached file, in order;
    `media_url`/`media_type` keep mirroring the first item for any existing
    client that only reads those flat fields.
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

    # Accept either the original single-file field or the new multi-file
    # one, but not neither/both — keeps the multipart contract unambiguous
    # while leaving existing single-`file` clients untouched.
    upload_files = [f for f in (files or []) if f is not None and f.filename]
    if file is not None and file.filename:
        upload_files = [file] + upload_files
    if not upload_files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Attach at least one file via `file` (single) or `files` (multiple)",
        )
    if media_captions is not None and len(media_captions) > len(upload_files):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="media_captions cannot contain more captions than uploaded files",
        )

    tag_ids = _parse_ids(tag_user_ids, "tag_user_ids")
    member_ids = _parse_ids(member_user_ids, "member_user_ids")

    if location_latitude is not None and not (-90 <= location_latitude <= 90):
        raise HTTPException(status_code=400, detail="location_latitude must be between -90 and 90")
    if location_longitude is not None and not (-180 <= location_longitude <= 180):
        raise HTTPException(status_code=400, detail="location_longitude must be between -180 and 180")

    try:
        location = resolve_location_from_form(
            db,
            location_id=location_id,
            location_name=location_name,
            location_address=location_address,
            location_city=location_city,
            location_state=location_state,
            location_country=location_country,
            location_latitude=location_latitude,
            location_longitude=location_longitude,
            location_place_id=location_place_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    saved = [save_upload_file(f, "posts", allow_video=True) for f in upload_files]
    first_url, first_kind = saved[0]
    post = models.Post(
        user_id=current_user.id,
        caption=caption,
        media_url=first_url,
        media_type=models.MediaType.video if first_kind == "video" else models.MediaType.image,
        alt_text=alt_text,
        ai_generated=ai_generated,
        music_title=music_title,
        music_artist=music_artist,
        music_url=music_url,
        music_start_seconds=music_start_seconds if music_title or music_url else None,
        # Flat columns stay populated for any existing reader of them
        # directly, alongside the richer Location row (location_id) below.
        location_name=location.name if location else location_name,
        location_latitude=location.latitude if location else location_latitude,
        location_longitude=location.longitude if location else location_longitude,
        location_id=location.id if location else None,
    )
    db.add(post)
    db.flush()

    for position, (url, kind) in enumerate(saved):
        db.add(models.PostMedia(
            post_id=post.id,
            media_url=url,
            media_type=models.MediaType.video if kind == "video" else models.MediaType.image,
            position=position,
            caption=media_captions[position] if media_captions and position < len(media_captions) else None,
        ))

    sync_post_hashtags(db, post, caption)

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
    """
    Posts from public accounts, people the current user follows, and their
    own posts — newest first. A private account's posts only show up here
    once the viewer is an approved follower (a pending FollowRequest does
    not count — see _visible_authors_clause / models.FollowRequest).
    """
    query = db.query(models.Post).join(models.User, models.Post.user_id == models.User.id)
    query = query.filter(_visible_authors_clause(db, current_user.id))
    muted_ids = muted_user_ids(db, current_user.id, for_stories=False)
    if muted_ids:
        query = query.filter(models.Post.user_id.notin_(muted_ids))
    total = query.count()
    posts = query.order_by(models.Post.created_at.desc()).offset(offset).limit(limit).all()
    items = [_to_post_detail(db, p, current_user.id) for p in posts]
    return schemas.PaginatedPostDetailResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/explore", response_model=schemas.PaginatedPostDetailResponse)
def get_explore_feed(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    days: int = Query(30, ge=1, le=90, description="Engagement window used for ranking, in days"),
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    """Posts from accounts the current user doesn't already follow (all
    posts, if logged out), ranked by recent engagement (likes + comments in
    the trailing `days` window) rather than plain recency — same idea as
    GET /api/reels/trending, just without the score>0 filter, so posts with
    no engagement yet still show up (ordered after everything that has some,
    newest first)."""
    viewer_id = current_user.id if current_user else None
    since = datetime.now(timezone.utc) - timedelta(days=days)

    likes_subq = (
        db.query(
            models.Like.target_id.label("post_id"),
            func.count(models.Like.id).label("like_score"),
        )
        .filter(
            models.Like.target_type == models.LikeTargetType.post,
            models.Like.created_at >= since,
        )
        .group_by(models.Like.target_id)
        .subquery()
    )
    comments_subq = (
        db.query(
            models.Comment.post_id.label("post_id"),
            func.count(models.Comment.id).label("comment_score"),
        )
        .filter(models.Comment.created_at >= since)
        .group_by(models.Comment.post_id)
        .subquery()
    )
    score = func.coalesce(likes_subq.c.like_score, 0) + func.coalesce(comments_subq.c.comment_score, 0)

    query = (
        db.query(models.Post, score.label("score"))
        .join(models.User, models.Post.user_id == models.User.id)
        .outerjoin(likes_subq, models.Post.id == likes_subq.c.post_id)
        .outerjoin(comments_subq, models.Post.id == comments_subq.c.post_id)
        .filter(_visible_authors_clause(db, viewer_id))
    )
    if current_user is not None:
        excluded_ids = _following_ids(db, current_user.id) + [current_user.id]
        query = query.filter(models.Post.user_id.notin_(excluded_ids))
    query = query.order_by(score.desc(), models.Post.created_at.desc())

    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    items = [_to_post_detail(db, p, viewer_id) for p, _score in rows]
    return schemas.PaginatedPostDetailResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/{post_id}", response_model=schemas.PostDetailOut)
def get_post(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    post = _get_post_or_404(db, post_id)
    viewer_id = current_user.id if current_user else None
    _require_author_visible(db, post.user, viewer_id)
    return _to_post_detail(db, post, viewer_id)


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
        db.flush()  # post.id already exists (update, not create), but keep
        # ordering explicit: hashtag sync reads/writes rows keyed on it.
        sync_post_hashtags(db, post, post.caption)

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
            post.location_id = None
        else:
            loc_row = find_or_create_location(
                db,
                name=location["name"],
                latitude=location.get("latitude"),
                longitude=location.get("longitude"),
            )
            post.location_name = loc_row.name
            post.location_latitude = loc_row.latitude
            post.location_longitude = loc_row.longitude
            post.location_id = loc_row.id

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
    file: UploadFile | None = File(default=None, description="Replace with a single file (unchanged behavior)."),
    files: list[UploadFile] | None = File(
        default=None, description="Replace with 2+ files instead, for a carousel post."
    ),
    media_captions: list[str] | None = Form(
        default=None, description="One caption per uploaded file, in the same order; trailing captions may be omitted."
    ),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Replace all of a post's media in one shot. Separate from PUT /:id
    because this needs multipart, not JSON. Full replace, same as before:
    whatever files are sent here become the post's entire media set —
    anything previously attached is deleted (DB rows and files on disk)."""
    post = _get_post_or_404(db, post_id)
    if post.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only update your own posts"
        )

    upload_files = [f for f in (files or []) if f is not None and f.filename]
    if file is not None and file.filename:
        upload_files = [file] + upload_files
    if not upload_files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Attach at least one file via `file` (single) or `files` (multiple)",
        )
    if media_captions is not None and len(media_captions) > len(upload_files):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="media_captions cannot contain more captions than uploaded files",
        )

    old_urls = [post.media_url] + [m.media_url for m in post.media_items]
    old_urls = list(dict.fromkeys(old_urls))  # de-dupe (media_items[0] often == media_url)

    saved = [save_upload_file(f, "posts", allow_video=True) for f in upload_files]
    first_url, first_kind = saved[0]
    post.media_url = first_url
    post.media_type = models.MediaType.video if first_kind == "video" else models.MediaType.image

    for m in list(post.media_items):
        db.delete(m)
    db.flush()
    for position, (url, kind) in enumerate(saved):
        db.add(models.PostMedia(
            post_id=post.id,
            media_url=url,
            media_type=models.MediaType.video if kind == "video" else models.MediaType.image,
            position=position,
            caption=media_captions[position] if media_captions and position < len(media_captions) else None,
        ))

    db.commit()
    db.refresh(post)
    for old_url in old_urls:
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

    media_urls = list(dict.fromkeys([post.media_url] + [m.media_url for m in post.media_items]))
    db.delete(post)  # cascades saved_posts/media_items/hashtag_rows via the ORM relationship
    db.commit()
    for url in media_urls:
        delete_media_file(url)
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
    location_name: str | None = Form(default=None),
    location_latitude: OptionalFloatForm(
        description="Optional. Leave unset (or blank) for no coordinates."
    ) = None,
    location_longitude: OptionalFloatForm(
        description="Optional. Leave unset (or blank) for no coordinates."
    ) = None,
    location_id: OptionalIntForm(
        description="Optional. Attach an already-saved location (see POST /api/locations) by id. "
        "Leave unset (or blank) if you're not tagging an existing saved location."
    ) = None,
    location_address: str | None = Form(default=None),
    location_city: str | None = Form(default=None),
    location_state: str | None = Form(default=None),
    location_country: str | None = Form(default=None),
    location_place_id: str | None = Form(default=None),
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

    Location works the same as POST /api/posts: pass an existing
    `location_id`, or `location_name`/the other `location_*` fields to
    resolve-or-create one, or leave all of them unset for no location.
    Changeable afterward too, via PUT /api/videos/{id}/metadata.
    """
    if location_latitude is not None and not (-90 <= location_latitude <= 90):
        raise HTTPException(status_code=400, detail="location_latitude must be between -90 and 90")
    if location_longitude is not None and not (-180 <= location_longitude <= 180):
        raise HTTPException(status_code=400, detail="location_longitude must be between -180 and 180")

    try:
        location = resolve_location_from_form(
            db,
            location_id=location_id,
            location_name=location_name,
            location_address=location_address,
            location_city=location_city,
            location_state=location_state,
            location_country=location_country,
            location_latitude=location_latitude,
            location_longitude=location_longitude,
            location_place_id=location_place_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    url, kind = save_upload_file(file, "reels", allow_video=True)
    if kind != "video":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Reels must be a video file"
        )

    # Extract and store the real video duration. This is used by WatchSession
    # validation so a viewer cannot accumulate more watch time than the reel
    # actually contains.
    duration_seconds = get_video_duration(url)

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
        user_id=current_user.id,
        caption=caption,
        video_url=url,
        thumbnail_url=thumbnail_url,
        duration_seconds=duration_seconds,
        location_name=location.name if location else location_name,
        location_latitude=location.latitude if location else location_latitude,
        location_longitude=location.longitude if location else location_longitude,
        location_id=location.id if location else None,
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
    muted_ids = set(muted_user_ids(db, current_user.id, for_stories=False))
    author_ids = [uid for uid in author_ids if uid not in muted_ids]
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
    viewer_id = current_user.id if current_user else None
    query = db.query(models.Reel).join(models.User, models.Reel.user_id == models.User.id)
    query = query.filter(_visible_authors_clause(db, viewer_id))
    total = query.count()
    reels = query.order_by(models.Reel.created_at.desc()).offset(offset).limit(limit).all()
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

    viewer_id = current_user.id if current_user else None

    query = (
        db.query(models.Reel, score.label("score"))
        .join(models.User, models.Reel.user_id == models.User.id)
        .outerjoin(likes_subq, models.Reel.id == likes_subq.c.reel_id)
        .outerjoin(watch_subq, models.Reel.id == watch_subq.c.reel_id)
        .filter(score > 0)
        .filter(_visible_authors_clause(db, viewer_id))
        .order_by(score.desc(), models.Reel.created_at.desc())
    )

    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    items = [_to_reel_detail(db, reel, viewer_id) for reel, _score in rows]
    return schemas.PaginatedReelDetailResponse(total=total, limit=limit, offset=offset, items=items)


@reels_router.get("/{reel_id}", response_model=schemas.ReelDetailOut)
def get_reel(
    reel_id: int,
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    reel = _get_reel_or_404(db, reel_id)
    viewer_id = current_user.id if current_user else None
    _require_author_visible(db, reel.user, viewer_id)
    return _to_reel_detail(db, reel, viewer_id)


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

    # Remove reel comments and their comment likes before deleting the reel.
    reel_comment_ids = [
        row[0] for row in db.query(models.Comment.id)
        .filter(models.Comment.reel_id == reel_id).all()
    ]
    if reel_comment_ids:
        db.query(models.Like).filter(
            models.Like.target_type == models.LikeTargetType.comment,
            models.Like.target_id.in_(reel_comment_ids),
        ).delete(synchronize_session=False)
        db.query(models.Comment).filter(
            models.Comment.parent_id.in_(reel_comment_ids)
        ).delete(synchronize_session=False)
        db.query(models.Comment).filter(
            models.Comment.id.in_(reel_comment_ids)
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

    # CreatorCollaborationRequest.reel_id is the last real foreign key into
    # reels, and the one that was still 500ing here: a reel that's ever been
    # the subject of a collaboration proposal couldn't be deleted at all.
    # The rows aren't dropped, because an accepted request may already have
    # paid out a CreatorEarning and that audit trail has to survive. Instead
    # the reel reference is nulled (the column is nullable by design — a
    # request can always stand alone as a general proposal), and anything
    # still pending is cancelled, since the reel it was proposing work on no
    # longer exists and accepting it would tag a collaborator onto nothing.
    db.query(models.CreatorCollaborationRequest).filter(
        models.CreatorCollaborationRequest.reel_id == reel_id,
        models.CreatorCollaborationRequest.status == models.CollaborationStatus.pending,
    ).update(
        {
            models.CreatorCollaborationRequest.status: models.CollaborationStatus.cancelled,
            models.CreatorCollaborationRequest.responded_at: datetime.now(timezone.utc),
        },
        synchronize_session=False,
    )
    db.query(models.CreatorCollaborationRequest).filter(
        models.CreatorCollaborationRequest.reel_id == reel_id
    ).update({models.CreatorCollaborationRequest.reel_id: None}, synchronize_session=False)

    # Bookmarks point at the reel informally (target_type/target_id), so
    # they don't raise a foreign key error — they just rot into entries that
    # render as blanks in GET /api/saved. Drop the SavedItem rows and their
    # collection memberships. Shares are deliberately left alone: a share is
    # a historical record of "I sent you this", not a live pointer.
    saved_item_ids = [
        row[0]
        for row in db.query(models.SavedItem.id)
        .filter(
            models.SavedItem.target_type == models.SavedItemType.reel,
            models.SavedItem.target_id == reel_id,
        )
        .all()
    ]
    if saved_item_ids:
        db.query(models.SavedCollectionItem).filter(
            models.SavedCollectionItem.saved_item_id.in_(saved_item_ids)
        ).delete(synchronize_session=False)
        db.query(models.SavedItem).filter(
            models.SavedItem.id.in_(saved_item_ids)
        ).delete(synchronize_session=False)

    video_url, thumbnail_url = reel.video_url, reel.thumbnail_url
    try:
        db.delete(reel)  # cascades collaborators + revenue splits via the ORM relationship
        db.commit()
    except IntegrityError:
        # Something still references the reel. Roll back so the cleanup
        # above doesn't get half-committed, leaving a reel that's had its
        # likes and comments stripped but is still there.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This reel can't be deleted because other records still reference it",
        )

    # Only unlink the media once the row is actually gone — deleting the
    # file first would leave a playable-looking reel with a dead video URL
    # if the commit failed.
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

    # Extract and store the real duration for the remixed reel too.
    duration_seconds = get_video_duration(url)

    remix = models.Reel(
        user_id=current_user.id,
        caption=caption,
        video_url=url,
        duration_seconds=duration_seconds,
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