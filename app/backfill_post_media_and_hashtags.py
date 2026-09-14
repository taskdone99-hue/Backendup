"""
One-off backfill for posts created before the post_media / hashtags tables
existed.

Base.metadata.create_all() (see app/main.py) already creates the new empty
tables automatically on next startup — this script only needs to run once
after that, to populate them for *existing* rows:

  - post_media: one row per existing post, copied from its media_url/
    media_type, at position 0. Posts created after this update already get
    their PostMedia row(s) written at creation time (see
    app/routers/content_routes.py create_post) — this is purely for the
    backlog of posts that predate that table.
  - hashtags / post_hashtags: (re)parses every existing post's caption and
    writes the Hashtag/PostHashtag rows content_routes.py would have
    written had the table existed at post-creation time.

Safe to run more than once — both steps only insert rows that are missing.

Usage:
    python -m app.backfill_post_media_and_hashtags
"""

from app.database import SessionLocal
from app import models
from app.services.hashtag_service import sync_post_hashtags


def backfill_post_media(db) -> int:
    posts_needing_media = (
        db.query(models.Post)
        .outerjoin(models.PostMedia, models.PostMedia.post_id == models.Post.id)
        .filter(models.PostMedia.id.is_(None))
        .all()
    )
    for post in posts_needing_media:
        db.add(models.PostMedia(
            post_id=post.id,
            media_url=post.media_url,
            media_type=post.media_type,
            position=0,
        ))
    db.commit()
    return len(posts_needing_media)


def backfill_hashtags(db) -> int:
    """(Re)syncs hashtag rows for every post with a caption. Returns how
    many posts actually had at least one #tag in their caption (posts with
    no hashtags are still processed, to clear out any stale rows, but
    aren't counted since there's nothing new for them)."""
    posts = db.query(models.Post).filter(models.Post.caption.isnot(None)).all()
    touched = 0
    for post in posts:
        tags = sync_post_hashtags(db, post, post.caption)
        if tags:
            touched += 1
    db.commit()
    return touched


if __name__ == "__main__":
    db = SessionLocal()
    try:
        media_count = backfill_post_media(db)
        print(f"post_media: backfilled {media_count} post(s)")
        hashtag_count = backfill_hashtags(db)
        print(f"hashtags: (re)synced {hashtag_count} post(s)")
    finally:
        db.close()
    print("Done")
