"""
Hashtag parsing + persistence.

Captions are still free text (no separate "hashtags" input field on
create/update post) — tags are parsed out of the caption automatically,
same as before. The difference from the old inline `_extract_hashtags`
helper in content_routes.py is that the parsed tags are now also persisted
(Hashtag + PostHashtag rows), so they're queryable: "posts for #tag" and
"trending hashtags" need real rows to group/count/order by, not just a
per-request regex pass over one post's caption.
"""

import re

from sqlalchemy.orm import Session

from app import models

_HASHTAG_PATTERN = re.compile(r"#(\w+)")

# Instagram-style cap - a caption stuffed with hundreds of tags is almost
# always spam, and it keeps a single post from dominating trending results.
MAX_HASHTAGS_PER_POST = 30


def extract_hashtags(caption: str | None) -> list[str]:
    """Pulls #tags out of a caption, de-duplicated case-insensitively
    (stored/returned lowercase), in first-seen order, capped at
    MAX_HASHTAGS_PER_POST."""
    if not caption:
        return []
    seen: dict[str, None] = {}
    for match in _HASHTAG_PATTERN.finditer(caption):
        tag = match.group(1).lower()
        seen.setdefault(tag, None)
        if len(seen) >= MAX_HASHTAGS_PER_POST:
            break
    return list(seen.keys())


def get_or_create_hashtag(db: Session, name: str) -> models.Hashtag:
    name = name.lower().lstrip("#")
    existing = db.query(models.Hashtag).filter(models.Hashtag.name == name).first()
    if existing is not None:
        return existing
    hashtag = models.Hashtag(name=name)
    db.add(hashtag)
    db.flush()
    return hashtag


def sync_post_hashtags(db: Session, post: models.Post, caption: str | None) -> list[str]:
    """Full replace: (re)parses `caption` and makes the post's PostHashtag
    rows match exactly what's in it now — same "replace" shape as
    _replace_post_tags/_replace_post_members in content_routes.py. Call
    this on both post create and any caption update. Returns the tag list
    (lowercase, no '#') for convenience."""
    tags = extract_hashtags(caption)

    existing_rows = (
        db.query(models.PostHashtag)
        .filter(models.PostHashtag.post_id == post.id)
        .all()
    )
    existing_by_name = {row.hashtag.name: row for row in existing_rows}

    for name, row in existing_by_name.items():
        if name not in tags:
            db.delete(row)

    for name in tags:
        if name not in existing_by_name:
            hashtag = get_or_create_hashtag(db, name)
            db.add(models.PostHashtag(post_id=post.id, hashtag_id=hashtag.id))

    return tags


def hashtag_posts_count(db: Session, hashtag: models.Hashtag) -> int:
    return (
        db.query(models.PostHashtag)
        .filter(models.PostHashtag.hashtag_id == hashtag.id)
        .count()
    )
