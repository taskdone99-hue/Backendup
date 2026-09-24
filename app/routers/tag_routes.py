"""
User tagging APIs beyond "tag someone on a post/reel" (those live in
post_details_routes.py and content_routes.py and share their rules through
app/services/tag_service.py):

  Tag controls   GET/PUT /api/tags/settings          approve manually / who can tag me
  Tag search     GET /api/tags/search                users you can tag
  Tagged feed    GET /api/users/{id}/tagged          posts + reels a user is tagged in
  Approval       GET /api/tags/pending               tags waiting on you
                 POST /api/tags/{type}/{id}/approve
                 POST /api/tags/{type}/{id}/decline  (removes the tag)
  Hide           POST/DELETE /api/tags/{type}/{id}/hide   hide from your profile
  Story tags     POST/GET/DELETE /api/stories/{id}/tags

`{type}` is post | reel | story.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, case, or_
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user, get_current_user_optional
from app.routers.content_routes import (
    _require_author_visible,
    _to_post_detail,
    _to_reel_detail,
    _visible_authors_clause,
)
from app.routers.story_routes import _active_story_query, _get_active_story_or_404
from app.routers.user_routes import _get_user_or_404, _require_content_visible
from app.services import tag_service
from app.services.privacy_service import blocked_user_ids, is_blocked

router = APIRouter(tags=["tags"])

ContentType = Literal["post", "reel", "story"]


# ==========================================================================
# Tag controls
# ==========================================================================

def _settings_out(row: models.UserTagSettings | None) -> schemas.TagSettingsOut:
    if row is None:
        return schemas.TagSettingsOut()
    return schemas.TagSettingsOut(
        approve_tags_manually=row.approve_tags_manually, allow_tags_from=row.allow_tags_from
    )


@router.get("/api/tags/settings", response_model=schemas.TagSettingsOut)
def get_tag_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return _settings_out(tag_service.get_settings(db, current_user.id))


@router.put("/api/tags/settings", response_model=schemas.TagSettingsOut)
def update_tag_settings(
    payload: schemas.TagSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Partial update. `approve_tags_manually`: new tags of you wait in
    GET /api/tags/pending until approved. `allow_tags_from`: everyone |
    following (only people you follow) | no_one. Turning approval off does
    not auto-approve tags that are already pending."""
    row = tag_service.get_settings(db, current_user.id)
    if row is None:
        row = models.UserTagSettings(user_id=current_user.id)
        db.add(row)
    if payload.approve_tags_manually is not None:
        row.approve_tags_manually = payload.approve_tags_manually
    if payload.allow_tags_from is not None:
        row.allow_tags_from = payload.allow_tags_from
    db.commit()
    db.refresh(row)
    return _settings_out(row)


# ==========================================================================
# Tag search
# ==========================================================================

@router.get("/api/tags/search", response_model=schemas.PaginatedUsersResponse)
def search_taggable_users(
    q: str = Query(..., min_length=1, max_length=50),
    content_type: ContentType | None = Query(
        default=None,
        description="With content_id: also drop people who couldn't see that content of yours",
    ),
    content_id: int | None = Query(default=None, gt=0),
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Find people to tag. Case-insensitive match on username / full name;
    people you follow come first. Left out: yourself, inactive accounts,
    anyone blocked either way, and anyone whose "who can tag me" setting
    rules you out — so a suggestion doesn't turn into a 403 on tagging."""
    if (content_type is None) != (content_id is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="content_type and content_id must be provided together",
        )

    like = f"%{q.strip()}%"
    S = models.UserTagSettings
    follows_me = db.query(models.Follow.follower_id).filter(
        models.Follow.following_id == current_user.id
    )
    i_follow = db.query(models.Follow.following_id).filter(
        models.Follow.follower_id == current_user.id
    )

    query = (
        db.query(models.User)
        .outerjoin(S, S.user_id == models.User.id)
        .filter(
            models.User.is_active.is_(True),
            models.User.id != current_user.id,
            or_(models.User.username.ilike(like), models.User.full_name.ilike(like)),
            or_(
                S.id.is_(None),
                S.allow_tags_from == models.TagPermission.everyone,
                and_(
                    S.allow_tags_from == models.TagPermission.following,
                    models.User.id.in_(follows_me),
                ),
            ),
        )
    )
    blocked = blocked_user_ids(db, current_user.id)
    if blocked:
        query = query.filter(models.User.id.notin_(blocked))

    if content_type is not None:
        content = db.query(tag_service.CONTENT_MODEL[content_type]).filter(
            tag_service.CONTENT_MODEL[content_type].id == content_id
        ).first()
        if content is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"{content_type.capitalize()} not found"
            )
        if content.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="You can only tag on your own content"
            )
        # Same audience rules as tag_service.can_view_content.
        if current_user.is_private:
            my_followers = db.query(models.Follow.follower_id).filter(
                models.Follow.following_id == current_user.id
            )
            query = query.filter(models.User.id.in_(my_followers))
        if (
            content_type == "story"
            and content.visibility == models.StoryVisibility.close_friends
        ):
            friends = db.query(models.CloseFriend.friend_id).filter(
                models.CloseFriend.owner_id == current_user.id
            )
            query = query.filter(models.User.id.in_(friends))

    total = query.count()
    rows = (
        query.order_by(
            case((models.User.id.in_(i_follow), 0), else_=1), models.User.username.asc()
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    followed = {
        r[0]
        for r in db.query(models.Follow.following_id)
        .filter(
            models.Follow.follower_id == current_user.id,
            models.Follow.following_id.in_([u.id for u in rows]),
        )
        .all()
    } if rows else set()
    items = []
    for u in rows:
        out = schemas.UserSummaryOut.model_validate(u)
        out.is_following = u.id in followed
        items.append(out)
    return schemas.PaginatedUsersResponse(total=total, limit=limit, offset=offset, items=items)


# ==========================================================================
# Tagged feed — "photos and reels of you"
# ==========================================================================

@router.get("/api/users/{user_id}/tagged", response_model=schemas.PaginatedTaggedResponse)
def get_user_tagged(
    user_id: int,
    content_type: Literal["all", "post", "reel"] = Query("all"),
    include_hidden: bool = Query(
        False, description="Only honored when viewing your own tagged feed"
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    """Posts and reels this user is tagged in, newest tag first. Shown to a
    viewer only if they can see the user's profile (private-account rule)
    AND could open each item themselves (author privacy / blocks). Left out:
    pending (unapproved) tags, tags the user hid from their profile (unless
    you pass include_hidden on your own feed), and the user's own content."""
    target = _get_user_or_404(db, user_id)
    viewer_id = current_user.id if current_user else None
    if viewer_id is not None and viewer_id != target.id and is_blocked(db, viewer_id, target.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    _require_content_visible(db, target, viewer_id)
    show_hidden = include_hidden and viewer_id == target.id

    def _base(Tag, Content):
        fk = tag_service.fk_column("post" if Content is models.Post else "reel")
        query = (
            db.query(Content.id, Tag.tagged_at, Tag.id.label("tag_id"))
            .select_from(Tag)
            .join(Content, Content.id == fk)
            .join(models.User, models.User.id == Content.user_id)
            .filter(
                Tag.user_id == user_id,
                Tag.is_approved.is_(True),
                Content.user_id != user_id,
            )
            .filter(_visible_authors_clause(db, viewer_id))
        )
        if not show_hidden:
            query = query.filter(Tag.hidden_from_profile.is_(False))
        return query

    sources = []
    if content_type in ("all", "post"):
        sources.append(("post", _base(models.PostTag, models.Post)))
    if content_type in ("all", "reel"):
        sources.append(("reel", _base(models.ReelTag, models.Reel)))

    total = 0
    entries = []  # (tagged_at, tag_id, type, content_id)
    for ctype, query in sources:
        total += query.count()
        rows = (
            query.order_by(_order_col(ctype).desc(), _tag_id_col(ctype).desc())
            .limit(offset + limit)
            .all()
        )
        entries.extend((r.tagged_at, r.tag_id, ctype, r[0]) for r in rows)

    entries.sort(key=lambda e: (e[0], e[1]), reverse=True)
    page = entries[offset:offset + limit]

    post_ids = [e[3] for e in page if e[2] == "post"]
    reel_ids = [e[3] for e in page if e[2] == "reel"]
    posts = {
        p.id: p for p in db.query(models.Post).filter(models.Post.id.in_(post_ids)).all()
    } if post_ids else {}
    reels = {
        r.id: r for r in db.query(models.Reel).filter(models.Reel.id.in_(reel_ids)).all()
    } if reel_ids else {}

    items = []
    for tagged_at, _tag_id, ctype, cid in page:
        if ctype == "post":
            items.append(schemas.TaggedItemOut(
                content_type="post", tagged_at=tagged_at,
                post=_to_post_detail(db, posts[cid], viewer_id),
            ))
        else:
            items.append(schemas.TaggedItemOut(
                content_type="reel", tagged_at=tagged_at,
                reel=_to_reel_detail(db, reels[cid], viewer_id),
            ))
    return schemas.PaginatedTaggedResponse(total=total, limit=limit, offset=offset, items=items)


def _order_col(ctype: str):
    return (models.PostTag if ctype == "post" else models.ReelTag).tagged_at


def _tag_id_col(ctype: str):
    return (models.PostTag if ctype == "post" else models.ReelTag).id


# ==========================================================================
# Approval / decline / hide
# ==========================================================================

def _preview_url(content_type: str, content) -> str | None:
    if content_type == "post":
        if content.media_items:
            return content.media_items[0].media_url
        return content.media_url
    if content_type == "reel":
        return content.thumbnail_url or content.video_url
    return content.media_url


@router.get("/api/tags/pending", response_model=schemas.PaginatedPendingTagsResponse)
def get_pending_tags(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Tags of you waiting for approval (you have "approve tags manually"
    on), newest first. Expired stories and content from anyone you've
    blocked (or who blocked you) are left out."""
    blocked = set(blocked_user_ids(db, current_user.id))
    entries = []

    for ctype in ("post", "reel"):
        Tag = tag_service.TAG_MODEL[ctype]
        Content = tag_service.CONTENT_MODEL[ctype]
        rows = (
            db.query(Tag, Content)
            .join(Content, Content.id == tag_service.fk_column(ctype))
            .filter(Tag.user_id == current_user.id, Tag.is_approved.is_(False))
            .all()
        )
        entries.extend((t, ctype, c) for t, c in rows)

    pending_story_tags = (
        db.query(models.StoryTag)
        .filter(models.StoryTag.user_id == current_user.id, models.StoryTag.is_approved.is_(False))
        .all()
    )
    if pending_story_tags:
        active = {
            s.id: s
            for s in _active_story_query(db)
            .filter(models.Story.id.in_([t.story_id for t in pending_story_tags]))
            .all()
        }
        entries.extend(
            (t, "story", active[t.story_id]) for t in pending_story_tags if t.story_id in active
        )

    entries = [e for e in entries if e[2].user_id not in blocked]
    entries.sort(key=lambda e: (e[0].tagged_at, e[0].id), reverse=True)
    total = len(entries)
    items = [
        schemas.PendingTagOut(
            content_type=ctype,
            content_id=content.id,
            owner=schemas.UserSummaryOut.model_validate(content.user),
            preview_url=_preview_url(ctype, content),
            tagged_at=tag.tagged_at,
        )
        for tag, ctype, content in entries[offset:offset + limit]
    ]
    return schemas.PaginatedPendingTagsResponse(total=total, limit=limit, offset=offset, items=items)


def _get_my_tag_or_404(db: Session, content_type: str, content_id: int, user_id: int):
    Tag = tag_service.TAG_MODEL[content_type]
    tag = (
        db.query(Tag)
        .filter(tag_service.fk_column(content_type) == content_id, Tag.user_id == user_id)
        .first()
    )
    if tag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")
    return tag


@router.post("/api/tags/{content_type}/{content_id}/approve", response_model=schemas.MessageResponse)
def approve_tag(
    content_type: ContentType,
    content_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Approve a pending tag of you so it shows on the content and in your
    Tagged tab. Idempotent."""
    tag = _get_my_tag_or_404(db, content_type, content_id, current_user.id)
    if not tag.is_approved:
        tag.is_approved = True
        db.commit()
    return schemas.MessageResponse(message="Tag approved")


@router.post("/api/tags/{content_type}/{content_id}/decline", response_model=schemas.MessageResponse)
def decline_tag(
    content_type: ContentType,
    content_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Decline a pending tag — or remove an approved one — of yourself.
    Deletes the tag (same effect as DELETE .../tags/{your_user_id})."""
    tag = _get_my_tag_or_404(db, content_type, content_id, current_user.id)
    db.delete(tag)
    db.commit()
    return schemas.MessageResponse(message="Tag removed")


@router.post("/api/tags/{content_type}/{content_id}/hide", response_model=schemas.MessageResponse)
def hide_tag_from_profile(
    content_type: ContentType,
    content_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Hide a tag from your profile's Tagged tab. The tag stays on the
    content itself; use decline to remove it."""
    tag = _get_my_tag_or_404(db, content_type, content_id, current_user.id)
    if not tag.hidden_from_profile:
        tag.hidden_from_profile = True
        db.commit()
    return schemas.MessageResponse(message="Tag hidden from your profile")


@router.delete("/api/tags/{content_type}/{content_id}/hide", response_model=schemas.MessageResponse)
def unhide_tag_from_profile(
    content_type: ContentType,
    content_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    tag = _get_my_tag_or_404(db, content_type, content_id, current_user.id)
    if tag.hidden_from_profile:
        tag.hidden_from_profile = False
        db.commit()
    return schemas.MessageResponse(message="Tag shown on your profile")


# ==========================================================================
# Story tags — tap-to-tag on a story (post/reel equivalents live with their
# content routes). Same rules via tag_service; owner-only, active stories.
# ==========================================================================

@router.post(
    "/api/stories/{story_id}/tags",
    response_model=schemas.StoryTagsResponse,
    status_code=status.HTTP_201_CREATED,
)
async def tag_story_people(
    story_id: int,
    payload: schemas.TagPeopleRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    story = _get_active_story_or_404(db, story_id, current_user.id)
    if story.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only tag people on your own story"
        )
    created = tag_service.add_tags(
        db, "story", story, current_user,
        [(t.user_id, t.x_position, t.y_position) for t in payload.tags],
    )
    db.commit()
    await tag_service.notify_new_tags(db, "story", story.id, current_user, created)
    return schemas.StoryTagsResponse(
        message="Tagged", tags=tag_service.visible_tags(db, "story", story, current_user.id)
    )


@router.get("/api/stories/{story_id}/tags", response_model=schemas.StoryTagsResponse)
def get_story_tags(
    story_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    story = _get_active_story_or_404(db, story_id, current_user.id)
    _require_author_visible(db, story.user, current_user.id)
    return schemas.StoryTagsResponse(
        message="", tags=tag_service.visible_tags(db, "story", story, current_user.id)
    )


@router.delete("/api/stories/{story_id}/tags/{user_id}", response_model=schemas.MessageResponse)
def remove_story_tag(
    story_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    story = _get_active_story_or_404(db, story_id, current_user.id)
    tag = (
        db.query(models.StoryTag)
        .filter(models.StoryTag.story_id == story_id, models.StoryTag.user_id == user_id)
        .first()
    )
    if tag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")
    # Same rule as posts/reels: the owner can remove any tag; the tagged
    # person can remove themselves.
    if current_user.id != story.user_id and current_user.id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the story owner or the tagged user can remove this tag",
        )
    db.delete(tag)
    db.commit()
    return schemas.MessageResponse(message="Tag removed")
