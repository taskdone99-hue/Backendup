"""
Deletes stories (and their story_views, via cascade) whose expires_at
passed more than ARCHIVE_RETENTION_DAYS ago. All story read-endpoints
already filter expired stories out of the public feed/viewers/mine
results on their own (see story_routes._active_story_query) — an expired
story just moves into GET /api/stories/archive (owner-only) until this
retention window runs out, matching Instagram's "story goes to your
archive when it expires" behavior. This script is what eventually reclaims
storage/rows for stories nobody will ever look at again.

Run this periodically as a cron job or an RDS/EventBridge scheduled task,
e.g. daily:

    python -m app.cleanup_expired_stories

It also deletes the underlying media file from local disk storage (see
app/services/media_service.py) — if you switch media storage to S3, swap
that part for an S3 delete_object call, or rely on an S3 lifecycle rule
instead and drop that line here.
"""

import os
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app import models
from app.services.media_service import delete_media_file

ARCHIVE_RETENTION_DAYS = int(os.getenv("STORY_ARCHIVE_RETENTION_DAYS", "90"))


def cleanup_expired_stories() -> int:
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=ARCHIVE_RETENTION_DAYS)
        expired = db.query(models.Story).filter(models.Story.expires_at <= cutoff).all()

        for story in expired:
            delete_media_file(story.media_url)
            db.delete(story)

        db.commit()
        return len(expired)
    finally:
        db.close()


if __name__ == "__main__":
    count = cleanup_expired_stories()
    print(f"Deleted {count} expired stor{'y' if count == 1 else 'ies'} past the archive retention window")
