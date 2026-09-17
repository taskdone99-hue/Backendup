"""
One-off data repair for the watch-time bug: existing WatchSession rows
that were auto-closed long after they actually started (app killed/
backgrounded/lost connectivity without a clean /watch/end) got credited
with the entire elapsed gap as `watch_seconds` — no upper bound was
enforced before this fix (see MAX_VALID_WATCH_SECONDS and the updated
_close_session in app/routers/watch_routes.py).

This retroactively flags any already-stored session above that same
threshold as invalid, so GET /api/monetization/status and GET /api/watch/*
stop counting them immediately — without deleting the rows (kept for
abuse/analytics review, same as short/invalid sessions always have been).

Safe to run more than once.

Usage:
    python -m app.fix_stale_watch_sessions
"""

from app.database import SessionLocal
from app import models
from app.routers.watch_routes import MAX_VALID_WATCH_SECONDS


def fix_stale_watch_sessions(db) -> int:
    stale = (
        db.query(models.WatchSession)
        .filter(
            models.WatchSession.is_valid.is_(True),
            models.WatchSession.watch_seconds > MAX_VALID_WATCH_SECONDS,
        )
        .all()
    )
    for session in stale:
        session.is_valid = False
    db.commit()
    return len(stale)


if __name__ == "__main__":
    db = SessionLocal()
    try:
        count = fix_stale_watch_sessions(db)
        print(f"Flagged {count} stale watch session(s) (watch_seconds > {MAX_VALID_WATCH_SECONDS}) as invalid")
    finally:
        db.close()
    print("Done")
