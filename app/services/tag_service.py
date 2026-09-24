"""
Shared rules for tagging users on posts, reels and stories, so every path
that creates a tag (POST /api/posts/{id}/tags, /api/reels/{id}/tags,
/api/stories/{id}/tags, and the `tag_user_ids` field on post create/update)
enforces the same thing:

  * the tagged user exists;
  * neither side has blocked the other;
  * the tagged user's "who can tag me" setting allows the tagger
    (models.UserTagSettings.allow_tags_from);
  * the tagged user could actually see the content (private account they
    don't follow, close-friends-only story they're not on, etc.) — otherwise
    the "you were tagged" notification would lead them to a 403/404;
  * if the tagged user has "approve tags manually" on, the tag is stored
    pending (is_approved=False) instead of going live, and they get a
    `tag_request` notification instead of a `tag` one.

Denials for blocks/settings share one generic message on purpose: the tagger
shouldn't be able to tell "this person blocked me" from "this person only
lets people they follow tag them".

Callers commit; then call notify_new_tags() with what add_tags() returned.
"""

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app import models
from app.services.notification_service import notify_user
from app.services.privacy_service import is_blocked, is_close_friend

CONTENT_TYPES = ("post", "reel", "story")

TAG_MODEL = {
    "post": models.PostTag,
    "reel": models.ReelTag,
    "story": models.StoryTag,
}
_FK_NAME = {"post": "post_id", "reel": "reel_id", "story": "story_id"}
CONTENT_MODEL = {
    "post": models.Post,
    "reel": models.Reel,
    "story": models.Story,
}

TAGGING_DENIED = "Can't tag one or more of these users"
TAGGING_NO_ACCESS = "Can't tag someone who can't see this content"


def fk_column(content_type: str):
    return getattr(TAG_MODEL[content_type], _FK_NAME[content_type])


def content_id_of_tag(content_type: str, tag) -> int:
    return getattr(tag, _FK_NAME[content_type])


def get_settings(db: Session, user_id: int) -> models.UserTagSettings | None:
    return (
        db.query(models.UserTagSettings)
        .filter(models.UserTagSettings.user_id == user_id)
        .first()
    )


def _follows(db: Session, follower_id: int, following_id: int) -> bool:
    return (
        db.query(models.Follow.id)
        .filter(
            models.Follow.follower_id == follower_id,
            models.Follow.following_id == following_id,
        )
        .first()
        is not None
    )


def can_view_content(db: Session, content_type: str, content, user_id: int) -> bool:
    """Could `user_id` open this post/reel/story? Mirrors the rules the
    read endpoints apply (block either way, private account, close-friends
    story)."""
    owner = content.user
    if user_id == owner.id:
        return True
    if is_blocked(db, owner.id, user_id):
        return False
    if owner.is_private and not _follows(db, user_id, owner.id):
        return False
    if (
        content_type == "story"
        and content.visibility == models.StoryVisibility.close_friends
        and not is_close_friend(db, owner.id, user_id)
    ):
        return False
    return True


def tagger_allowed_by_settings(
    db: Session, tagger_id: int, tagged_id: int, settings: models.UserTagSettings | None
) -> bool:
    if is_blocked(db, tagger_id, tagged_id):
        return False
    if settings is None or settings.allow_tags_from == models.TagPermission.everyone:
        return True
    if settings.allow_tags_from == models.TagPermission.no_one:
        return False
    # "following": the tagged user must follow the tagger.
    return _follows(db, tagged_id, tagger_id)


def assert_can_tag(
    db: Session, tagger: models.User, content_type: str, content, user_ids: list[int]
) -> None:
    """Raises 404 (unknown user ids) or 403 (not allowed). Tagging yourself
    is always allowed."""
    if not user_ids:
        return
    found = {
        row[0] for row in db.query(models.User.id).filter(models.User.id.in_(user_ids)).all()
    }
    missing = [uid for uid in user_ids if uid not in found]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User(s) not found: {', '.join(str(m) for m in missing)}",
        )

    settings_by_user = {
        s.user_id: s
        for s in db.query(models.UserTagSettings)
        .filter(models.UserTagSettings.user_id.in_(user_ids))
        .all()
    }
    others = [uid for uid in user_ids if uid != tagger.id]
    for uid in others:
        if not tagger_allowed_by_settings(db, tagger.id, uid, settings_by_user.get(uid)):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=TAGGING_DENIED)
    for uid in others:
        if not can_view_content(db, content_type, content, uid):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=TAGGING_NO_ACCESS)


def add_tags(
    db: Session,
    content_type: str,
    content,
    tagger: models.User,
    entries: list[tuple[int, float | None, float | None]],
) -> list:
    """Tag `entries` = [(user_id, x, y), ...] on the content. Users already
    tagged are skipped (and not re-validated). All-or-nothing: if any new
    user fails validation nothing is added. Returns only the newly created
    tag rows (flushed, not committed)."""
    Tag = TAG_MODEL[content_type]
    fk = fk_column(content_type)
    ids = list(dict.fromkeys(uid for uid, _, _ in entries))

    already = {
        row[0]
        for row in db.query(Tag.user_id).filter(fk == content.id, Tag.user_id.in_(ids)).all()
    }
    new_entries = [e for e in entries if e[0] not in already]
    new_ids = list(dict.fromkeys(uid for uid, _, _ in new_entries))
    assert_can_tag(db, tagger, content_type, content, new_ids)

    settings_by_user = {
        s.user_id: s
        for s in db.query(models.UserTagSettings)
        .filter(models.UserTagSettings.user_id.in_(new_ids))
        .all()
    } if new_ids else {}

    created = []
    seen: set[int] = set()
    for uid, x, y in new_entries:
        if uid in seen:
            continue
        seen.add(uid)
        s = settings_by_user.get(uid)
        needs_approval = uid != tagger.id and s is not None and s.approve_tags_manually
        tag = Tag(user_id=uid, x_position=x, y_position=y, is_approved=not needs_approval)
        setattr(tag, _FK_NAME[content_type], content.id)
        db.add(tag)
        created.append(tag)
    db.flush()
    return created


def replace_post_tags(
    db: Session, post: models.Post, user_ids: list[int], tagger: models.User
) -> list[models.PostTag]:
    """Full replace for post create/update's `tag_user_ids`: tags anyone new
    on the list (through the same checks as above), untags anyone left off.
    Returns the newly created tags so the caller can notify after commit."""
    ids = list(dict.fromkeys(user_ids))
    existing = {
        t.user_id: t
        for t in db.query(models.PostTag).filter(models.PostTag.post_id == post.id).all()
    }
    created = add_tags(db, "post", post, tagger, [(uid, None, None) for uid in ids])
    for uid in set(existing) - set(ids):
        db.delete(existing[uid])
    return created


async def notify_new_tags(
    db: Session, content_type: str, content_id: int, actor: models.User, new_tags: list
) -> None:
    """Call after committing add_tags(). Goes through notify_user, so the
    tagged user's mention-notification preference applies."""
    for tag in new_tags:
        if tag.user_id == actor.id:
            continue
        if tag.is_approved:
            notif_type = models.NotificationType.tag
            message = f"{actor.username} tagged you in a {content_type}"
        else:
            notif_type = models.NotificationType.tag_request
            message = (
                f"{actor.username} wants to tag you in a {content_type} "
                "— approve it to show it on your profile"
            )
        await notify_user(
            db,
            user_id=tag.user_id,
            actor=actor,
            notif_type=notif_type,
            message=message,
            target_type=content_type,
            target_id=content_id,
        )


def visible_tags(db: Session, content_type: str, content, viewer_id: int | None) -> list:
    """Tags a given viewer may see on this content: everyone sees approved
    tags; pending ones are only visible to the content owner and to the
    tagged user themselves."""
    Tag = TAG_MODEL[content_type]
    rows = (
        db.query(Tag)
        .filter(fk_column(content_type) == content.id)
        .order_by(Tag.tagged_at, Tag.id)
        .all()
    )
    if viewer_id is not None and viewer_id == content.user_id:
        return rows
    return [t for t in rows if t.is_approved or (viewer_id is not None and t.user_id == viewer_id)]


def approved_tags(db: Session, content_type: str, content_id: int) -> list:
    Tag = TAG_MODEL[content_type]
    return (
        db.query(Tag)
        .filter(fk_column(content_type) == content_id, Tag.is_approved.is_(True))
        .order_by(Tag.tagged_at, Tag.id)
        .all()
    )
