"""
@mention parsing + persistence for posts, reels, and comments (stories have
their own explicit mention-by-user-id flow — see
app/services/story_extras_service.py — and are untouched here).

Same shape as app/services/hashtag_service.py: captions/comments stay free
text, @usernames are parsed out automatically and persisted as Mention
rows so "who mentioned me" is a real, queryable, notifiable thing instead
of just highlighted text in the client.
"""

import re

from sqlalchemy.orm import Session

from app import models

_MENTION_PATTERN = re.compile(r"@(\w+)")

# Same reasoning as hashtag_service.MAX_HASHTAGS_PER_POST — caps abuse and
# keeps a single caption from spamming half the user base with
# notifications.
MAX_MENTIONS_PER_TARGET = 20


def extract_mentions(text: str | None) -> list[str]:
    """Pulls @usernames out of text, de-duplicated case-insensitively
    (returned lowercase), in first-seen order, capped at
    MAX_MENTIONS_PER_TARGET."""
    if not text:
        return []
    seen: dict[str, None] = {}
    for match in _MENTION_PATTERN.finditer(text):
        username = match.group(1).lower()
        seen.setdefault(username, None)
        if len(seen) >= MAX_MENTIONS_PER_TARGET:
            break
    return list(seen.keys())


def sync_mentions(
    db: Session,
    target_type: models.MentionTargetType,
    target_id: int,
    text: str | None,
    author_id: int,
) -> list[models.User]:
    """Full replace, same "replace" shape as hashtag_service.sync_post_hashtags
    — (re)parses `text` and makes this target's Mention rows match exactly
    what's in it now. Call this on create AND on any caption/content edit,
    so an edit that removes an @mention also removes the stale row/never
    re-notifies it, and one that adds a new @mention picks it up.

    Returns only the users newly mentioned by this call (not ones already
    mentioned before this edit) — that's the list the caller should send
    "X mentioned you" notifications for, so editing a caption back and
    forth doesn't spam the same person repeatedly.
    """
    usernames = extract_mentions(text)
    # Self-mentions don't get a row or a notification — same reasoning as
    # story_extras_service.attach_mentions skipping self-mentions.
    author = db.query(models.User).filter(models.User.id == author_id).first()
    author_username = author.username.lower() if author else None

    matched_users = (
        db.query(models.User)
        .filter(models.User.username.in_(usernames))
        .all()
        if usernames
        else []
    )
    matched_by_username = {u.username.lower(): u for u in matched_users}

    existing_rows = (
        db.query(models.Mention)
        .filter(models.Mention.target_type == target_type, models.Mention.target_id == target_id)
        .all()
    )
    existing_user_ids = {row.user_id for row in existing_rows}
    wanted_users = {
        u.id: u for name, u in matched_by_username.items() if name != author_username
    }

    for row in existing_rows:
        if row.user_id not in wanted_users:
            db.delete(row)

    newly_mentioned: list[models.User] = []
    for user_id, user in wanted_users.items():
        if user_id not in existing_user_ids:
            db.add(models.Mention(target_type=target_type, target_id=target_id, user_id=user_id))
            newly_mentioned.append(user)

    return newly_mentioned
