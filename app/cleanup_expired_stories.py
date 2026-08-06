"""
Deletes stories (and their story_views, via cascade) whose expires_at has
passed. All story read-endpoints already filter out expired stories on
their own, so this script isn't required for correctness — it just reclaims
storage/rows on a schedule.

Run this periodically as a cron job or an RDS/EventBridge scheduled task,
e.g. hourly:

    python -m app.cleanup_expired_stories

It also deletes the underlying media file from local disk storage (see
app/services/media_service.py) — if you switch media storage to S3, swap
that part for an S3 delete_object call, or rely on an S3 lifecycle rule
instead and drop that line here.
"""

from datetime import datetime, timezone

from app.database import SessionLocal
from app import models
from app.services.media_service import delete_media_file


def cleanup_expired_stories() -> int:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        expired = db.query(models.Story).filter(models.Story.expires_at <= now).all()

        for story in expired:
            delete_media_file(story.media_url)
            db.delete(story)

        db.commit()
        return len(expired)
    finally:
        db.close()


if __name__ == "__main__":
    count = cleanup_expired_stories()
    print(f"Deleted {count} expired stor{'y' if count == 1 else 'ies'}")
