"""
Reel watch-time tracking.

Tracks only the time a user actually spends watching a reel — the interval
between a /watch/start and the matching /watch/end — not how long the app
was open. The frontend is expected to call:

  * POST /api/watch/start  when a reel begins playing
  * POST /api/watch/end    when the user scrolls away, pauses, or leaves
                            the app (whichever happens first)

Design notes:

  * Timestamps are always taken from the server clock, never from the
    client, so a modified app can't report inflated watch times.
  * At most one *active* (started, not yet ended) session is allowed per
    user. If /watch/start is called while one is already open — e.g. the
    frontend missed sending /watch/end before the next reel started — the
    old session is auto-closed first using "now" as its end time, exactly
    as if the user had scrolled away at that instant. This keeps the API
    resilient to imperfect frontend event ordering instead of just 409ing.
  * Sessions shorter than MIN_VALID_WATCH_SECONDS are kept in the table
    (useful for abuse/analytics review) but flagged `is_valid=False` and
    excluded from history and stats.
"""

import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import get_current_user
from app.database import get_db

router = APIRouter(prefix="/api/watch", tags=["watch"])

# Sessions shorter than this are noise (accidental taps, fast scroll-throughs)
# and are excluded from history/stats rather than counted as a "watch".
MIN_VALID_WATCH_SECONDS = int(os.getenv("MIN_VALID_WATCH_SECONDS", "2"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _close_session(session: models.WatchSession, ended_at: datetime) -> None:
    """Stamp a session as ended in place. Caller commits."""

    started_at = session.started_at

    # Normalize timezone from DB
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)

    if ended_at.tzinfo is None:
        ended_at = ended_at.replace(tzinfo=timezone.utc)
        

    watch_seconds = max(0, int((ended_at - started_at).total_seconds()))

    session.ended_at = ended_at
    session.watch_seconds = watch_seconds
    session.active_owner_id = None
    session.is_valid = watch_seconds >= MIN_VALID_WATCH_SECONDS


@router.post("/start", response_model=schemas.WatchStartResponse, status_code=status.HTTP_201_CREATED)
def start_watch(
    body: schemas.WatchStartRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    reel = db.query(models.Reel).filter(models.Reel.id == body.reel_id).first()
    if reel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reel not found")

    now = _now()

    # Lock the user's active session row (if any) so a concurrent
    # start/end for the same user can't race with this one.
    existing_active = (
        db.query(models.WatchSession)
        .filter(models.WatchSession.active_owner_id == current_user.id)
        .with_for_update()
        .first()
    )
    if existing_active is not None:
        _close_session(existing_active, now)

    session = models.WatchSession(
        user_id=current_user.id,
        reel_id=body.reel_id,
        started_at=now,
        active_owner_id=current_user.id,
        is_valid=True,
    )
    db.add(session)
    try:
        db.commit()
    except IntegrityError:
        # Belt-and-braces: two /watch/start calls for the same user landed
        # concurrently and both passed the check above.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A watch session is already active for this user",
        )
    db.refresh(session)

    return schemas.WatchStartResponse(
        session_id=session.id, reel_id=session.reel_id, started_at=session.started_at
    )


@router.post("/end", response_model=schemas.WatchEndResponse)
def end_watch(
    body: schemas.WatchEndRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    session = (
        db.query(models.WatchSession)
        .filter(
            models.WatchSession.id == body.session_id,
            models.WatchSession.user_id == current_user.id,
        )
        .with_for_update()
        .first()
    )
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Watch session not found")
    if session.ended_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Watch session already ended")

    _close_session(session, _now())
    db.commit()
    db.refresh(session)

    return schemas.WatchEndResponse(
        session_id=session.id,
        reel_id=session.reel_id,
        watch_seconds=session.watch_seconds,
        counted=session.is_valid,
    )


@router.get("/history", response_model=schemas.PaginatedWatchHistoryResponse)
def get_watch_history(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    query = db.query(models.WatchSession).filter(
        models.WatchSession.user_id == current_user.id,
        models.WatchSession.is_valid.is_(True),
        models.WatchSession.ended_at.isnot(None),
    )
    total = query.count()
    rows = query.order_by(models.WatchSession.started_at.desc()).offset(offset).limit(limit).all()

    items = [
        schemas.WatchHistoryItem(
            session_id=row.id,
            reel_id=row.reel_id,
            started_at=row.started_at,
            ended_at=row.ended_at,
            watch_seconds=row.watch_seconds,
        )
        for row in rows
    ]
    return schemas.PaginatedWatchHistoryResponse(total=total, limit=limit, offset=offset, items=items)


def _period_stats(db: Session, user_id: int, since: datetime | None) -> schemas.WatchPeriodStats:
    query = db.query(
        func.coalesce(func.sum(models.WatchSession.watch_seconds), 0),
        func.count(models.WatchSession.id),
    ).filter(
        models.WatchSession.user_id == user_id,
        models.WatchSession.is_valid.is_(True),
        models.WatchSession.ended_at.isnot(None),
    )
    if since is not None:
        query = query.filter(models.WatchSession.started_at >= since)

    watch_seconds, reels_watched = query.one()
    return schemas.WatchPeriodStats(watch_seconds=int(watch_seconds), reels_watched=int(reels_watched))


@router.get("/stats", response_model=schemas.WatchStatsResponse)
def get_watch_stats(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    now = _now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = now - timedelta(days=7)
    month_start = now - timedelta(days=30)

    return schemas.WatchStatsResponse(
        today=_period_stats(db, current_user.id, today_start),
        week=_period_stats(db, current_user.id, week_start),
        month=_period_stats(db, current_user.id, month_start),
        total=_period_stats(db, current_user.id, None),
    )
