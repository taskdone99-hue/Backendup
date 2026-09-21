"""
Privacy: block, restrict, and mute — the DB-facing helpers shared by
privacy_routes.py and the read/write paths elsewhere in the app that need
to respect them (content_routes, story_routes, comment_routes, chat_routes).

These three are deliberately different shapes, matching real Instagram:
  - block:   symmetric effect, silent to nobody (both sides know — the
             blocked user just can't find/interact with the blocker).
  - restrict: one-directional and silent (the restricted user is never
             told). Narrow effect: their comments become restricter-only
             visible, their DMs become message requests.
  - mute:    one-directional, silent, and *only* affects the muter's own
             follows-scoped feeds (Home feed, Home reels, story tray) —
             explore/hashtag/search results are unaffected, same as
             Instagram.
"""

from sqlalchemy.orm import Session

from app import models


# ---- block ----

def is_blocked(db: Session, user_a_id: int, user_b_id: int) -> bool:
    """True if either has blocked the other."""
    return (
        db.query(models.UserBlock)
        .filter(
            models.UserBlock.blocker_id.in_([user_a_id, user_b_id]),
            models.UserBlock.blocked_id.in_([user_a_id, user_b_id]),
        )
        .first()
        is not None
    )


def blocked_user_ids(db: Session, user_id: int) -> list[int]:
    """Everyone `user_id` has blocked OR who has blocked `user_id` —
    the full exclusion set for "don't show me this person / don't show
    them me", used by content_routes._visible_authors_clause."""
    blocked_by_me = {
        row[0]
        for row in db.query(models.UserBlock.blocked_id)
        .filter(models.UserBlock.blocker_id == user_id)
        .all()
    }
    blocked_me = {
        row[0]
        for row in db.query(models.UserBlock.blocker_id)
        .filter(models.UserBlock.blocked_id == user_id)
        .all()
    }
    return list(blocked_by_me | blocked_me)


# ---- restrict ----

def is_restricted(db: Session, restricter_id: int, restricted_id: int) -> bool:
    """True if `restricter_id` has specifically restricted `restricted_id`
    (one-directional — restricted_id restricting restricter_id back, if
    they even know to, doesn't count here)."""
    return (
        db.query(models.UserRestrict)
        .filter(
            models.UserRestrict.restricter_id == restricter_id,
            models.UserRestrict.restricted_id == restricted_id,
        )
        .first()
        is not None
    )


def restricted_by_ids(db: Session, restricted_id: int) -> list[int]:
    """Everyone who has restricted `restricted_id` — used to decide whether
    a given viewer should see a restricted user's comment on THAT viewer's
    own post (see comment_routes.get_comments)."""
    return [
        row[0]
        for row in db.query(models.UserRestrict.restricter_id)
        .filter(models.UserRestrict.restricted_id == restricted_id)
        .all()
    ]


def restricted_user_ids(db: Session, restricter_id: int) -> list[int]:
    """Everyone `restricter_id` has restricted — used to hide their
    comments from other viewers of restricter_id's own posts/reels (see
    comment_routes.get_comments / get_reel_comments)."""
    return [
        row[0]
        for row in db.query(models.UserRestrict.restricted_id)
        .filter(models.UserRestrict.restricter_id == restricter_id)
        .all()
    ]


# ---- mute ----

def muted_user_ids(db: Session, muter_id: int, *, for_stories: bool = False) -> list[int]:
    """Who `muter_id` has muted, for posts (default) or stories."""
    column = models.UserMute.mute_stories if for_stories else models.UserMute.mute_posts
    return [
        row[0]
        for row in db.query(models.UserMute.muted_id)
        .filter(models.UserMute.muter_id == muter_id, column.is_(True))
        .all()
    ]


def get_mute(db: Session, muter_id: int, muted_id: int) -> models.UserMute | None:
    return (
        db.query(models.UserMute)
        .filter(models.UserMute.muter_id == muter_id, models.UserMute.muted_id == muted_id)
        .first()
    )


# ---- conversation mute ----

def is_conversation_muted(db: Session, user_id: int, conversation_id: int) -> bool:
    return (
        db.query(models.ConversationMute)
        .filter(
            models.ConversationMute.user_id == user_id,
            models.ConversationMute.conversation_id == conversation_id,
        )
        .first()
        is not None
    )


# ---- close friends ----

def is_close_friend(db: Session, owner_id: int, friend_id: int) -> bool:
    """True if `owner_id` has added `friend_id` to their Close Friends list
    — used to gate visibility of a close_friends-only story (see
    story_routes._viewer_can_see_story)."""
    return (
        db.query(models.CloseFriend)
        .filter(models.CloseFriend.owner_id == owner_id, models.CloseFriend.friend_id == friend_id)
        .first()
        is not None
    )
