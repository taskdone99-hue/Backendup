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

  * At most one active (started, not yet ended) session is allowed per
    user. If /watch/start is called while one is already open — e.g. the
    frontend missed sending /watch/end before the next reel started — the
    old session is auto-closed first using "now" as its end time, exactly
    as if the user had scrolled away at that instant.

  * Sessions shorter than MIN_VALID_WATCH_SECONDS are kept in the table
    but flagged is_valid=False and excluded from history and stats.

  * Sessions longer than MAX_VALID_WATCH_SECONDS are also kept in the table
    but flagged is_valid=False. Their full elapsed duration is retained for
    audit/debugging instead of being silently reduced to MAX_VALID_WATCH_SECONDS.

  * For normal sessions, credited watch time cannot exceed the actual Reel
    duration when duration_seconds is available.

  * This prevents a 10-second Reel from contributing more than 10 seconds
    of valid watch time while also preserving stale/abandoned sessions as
    invalid records rather than turning them into valid 600-second sessions.
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
MIN_VALID_WATCH_SECONDS = int(
    os.getenv("MIN_VALID_WATCH_SECONDS", "2")
)


# Sessions longer than this are almost certainly stale/abandoned rather than
# real engagement.
#
# Example:
# User starts watching a 10-second Reel and then kills the app.
# No /watch/end request reaches the backend.
# The next day the user starts another Reel.
#
# The old session is auto-closed using the server's current time. Its elapsed
# duration may therefore be ~93,000 seconds.
#
# We intentionally DO NOT clamp such a session down to 600 seconds.
# Instead, we preserve its actual elapsed duration and mark it invalid:
#
#     watch_seconds = ~93000
#     is_valid = False
#
# _period_stats() only counts is_valid=True sessions, so stale sessions cannot
# inflate monetization watch time.
MAX_VALID_WATCH_SECONDS = int(
    os.getenv("MAX_VALID_WATCH_SECONDS", "600")
)


def _now() -> datetime:
    """Return the current UTC server time."""
    return datetime.now(timezone.utc)


def _close_session(
    db: Session,
    session: models.WatchSession,
    ended_at: datetime,
) -> None:
    """
    Close a watch session.

    Rules:

    1. Calculate elapsed time using server timestamps.
    2. If the session is within MAX_VALID_WATCH_SECONDS and the Reel has a
       known duration, never credit more than the Reel's actual duration.
    3. If the session itself exceeds MAX_VALID_WATCH_SECONDS, preserve the
       full elapsed value and mark the session invalid.
    4. Never allow a stale session to become valid merely because its value
       was clamped to the maximum.
    """

    started_at = session.started_at

    # SQLAlchemy/MySQL may return a naive datetime depending on configuration.
    # Treat naive database timestamps as UTC.
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)

    if ended_at.tzinfo is None:
        ended_at = ended_at.replace(tzinfo=timezone.utc)

    # Server-side elapsed duration.
    elapsed_seconds = max(
        0,
        int((ended_at - started_at).total_seconds()),
    )

    # Start with the real elapsed time.
    watch_seconds = elapsed_seconds

    # Fetch the Reel so we can compare the watch session with the actual
    # video duration.
    reel = (
        db.query(models.Reel)
        .filter(models.Reel.id == session.reel_id)
        .first()
    )

    # IMPORTANT:
    #
    # Only apply Reel-duration capping when the session itself is within
    # the allowed maximum.
    #
    # Example:
    #
    # 10-second Reel + 8-second session
    #     => 8 seconds
    #
    # 10-second Reel + 14-second session
    #     => 10 seconds
    #
    # 10-second Reel + 25-hour abandoned session
    #     => ~90000+ seconds, INVALID
    #
    # We must not turn the 25-hour session into 600 seconds because that
    # would make it appear valid.
    if elapsed_seconds <= MAX_VALID_WATCH_SECONDS:
        if reel is not None and reel.duration_seconds is not None:
            duration_seconds = max(
                0,
                int(reel.duration_seconds),
            )

            watch_seconds = min(
                elapsed_seconds,
                duration_seconds,
            )

    # IMPORTANT:
    #
    # Do NOT do:
    #
    #     watch_seconds = min(watch_seconds, MAX_VALID_WATCH_SECONDS)
    #
    # because that converts an invalid 601-second session into exactly
    # 600 seconds, which would incorrectly make it valid.
    #
    # Instead, the actual value is retained and is_valid determines whether
    # it contributes to history/statistics/monetization.

    session.ended_at = ended_at
    session.watch_seconds = watch_seconds
    session.active_owner_id = None

    session.is_valid = (
        MIN_VALID_WATCH_SECONDS
        <= watch_seconds
        <= MAX_VALID_WATCH_SECONDS
    )


@router.post(
    "/start",
    response_model=schemas.WatchStartResponse,
    status_code=status.HTTP_201_CREATED,
)
def start_watch(
    body: schemas.WatchStartRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Start a Reel watch session.

    If the current user already has an active session, close that session
    first using the current server time.
    """

    reel = (
        db.query(models.Reel)
        .filter(models.Reel.id == body.reel_id)
        .first()
    )

    if reel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Reel not found",
        )

    now = _now()

    # Lock the user's active session row, if one exists.
    #
    # This prevents concurrent start requests for the same user from
    # accidentally creating overlapping active sessions.
    existing_active = (
        db.query(models.WatchSession)
        .filter(
            models.WatchSession.active_owner_id == current_user.id
        )
        .with_for_update()
        .first()
    )

    if existing_active is not None:
        _close_session(
            db,
            existing_active,
            now,
        )

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
        # Belt-and-braces protection for two concurrent /watch/start
        # requests that both pass the active-session query.
        db.rollback()

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A watch session is already active for this user",
        )

    db.refresh(session)

    return schemas.WatchStartResponse(
        session_id=session.id,
        reel_id=session.reel_id,
        started_at=session.started_at,
    )


@router.post(
    "/end",
    response_model=schemas.WatchEndResponse,
)
def end_watch(
    body: schemas.WatchEndRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    End an existing watch session.

    The end timestamp always comes from the server clock.
    """

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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Watch session not found",
        )

    if session.ended_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Watch session already ended",
        )

    _close_session(
        db,
        session,
        _now(),
    )

    db.commit()
    db.refresh(session)

    return schemas.WatchEndResponse(
        session_id=session.id,
        reel_id=session.reel_id,
        watch_seconds=session.watch_seconds,
        counted=session.is_valid,
    )


@router.get(
    "/history",
    response_model=schemas.PaginatedWatchHistoryResponse,
)
def get_watch_history(
    limit: int = Query(
        20,
        ge=1,
        le=100,
    ),
    offset: int = Query(
        0,
        ge=0,
    ),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Return valid completed watch sessions for the current user.
    """

    query = (
        db.query(models.WatchSession)
        .filter(
            models.WatchSession.user_id == current_user.id,
            models.WatchSession.is_valid.is_(True),
            models.WatchSession.ended_at.isnot(None),
        )
    )

    total = query.count()

    rows = (
        query
        .order_by(
            models.WatchSession.started_at.desc()
        )
        .offset(offset)
        .limit(limit)
        .all()
    )

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

    # All-time valid watch statistics.
    all_time = _period_stats(
        db,
        current_user.id,
        None,
    )

    return schemas.PaginatedWatchHistoryResponse(
        total=total,
        limit=limit,
        offset=offset,
        total_watch_seconds=all_time.watch_seconds,
        total_reels_watched=all_time.reels_watched,
        items=items,
    )


def _period_stats(
    db: Session,
    user_id: int,
    since: datetime | None,
) -> schemas.WatchPeriodStats:
    """
    Calculate valid watch-time statistics.

    Only sessions that are:
      - owned by the requested user
      - completed
      - marked is_valid=True

    are included.

    This is the important protection that keeps stale/abandoned sessions
    from contributing to monetization.
    """

    query = db.query(
        func.coalesce(
            func.sum(models.WatchSession.watch_seconds),
            0,
        ),
        func.count(models.WatchSession.id),
    ).filter(
        models.WatchSession.user_id == user_id,
        models.WatchSession.is_valid.is_(True),
        models.WatchSession.ended_at.isnot(None),
    )

    if since is not None:
        query = query.filter(
            models.WatchSession.started_at >= since
        )

    watch_seconds, reels_watched = query.one()

    return schemas.WatchPeriodStats(
        watch_seconds=int(watch_seconds),
        reels_watched=int(reels_watched),
    )


def _owner_period_stats(
    db: Session,
    owner_id: int,
    since: datetime | None,
) -> schemas.WatchPeriodStats:
    """
    Calculate valid watch-time credited toward a Reel owner's monetization.

    This is the "who does the watch time belong to" counterpart to
    _period_stats() above. _period_stats() answers "how much has this user
    watched"; this answers "how much watch time has this user *earned* as
    a creator" — i.e. time other people spent watching Reels this user
    posted.

    A row is credited to owner_id's monetization total only when:
      - the WatchSession's reel.user_id == owner_id (it's their Reel)
      - the WatchSession's viewer (user_id) is NOT owner_id — a creator
        watching their own Reel never counts toward their own
        monetization, no matter how long the session
      - the session is completed and marked is_valid=True (the same
        server-side validity check _close_session() already applies:
        excludes too-short sessions, and excludes stale/abandoned
        sessions auto-closed with an inflated elapsed duration)

    Joins against Reel (instead of trusting anything client-supplied) so
    ownership is always read from the current reel.user_id at query time.
    """

    query = (
        db.query(
            func.coalesce(
                func.sum(models.WatchSession.watch_seconds),
                0,
            ),
            func.count(models.WatchSession.id),
        )
        .join(
            models.Reel,
            models.Reel.id == models.WatchSession.reel_id,
        )
        .filter(
            models.Reel.user_id == owner_id,
            models.WatchSession.user_id != owner_id,
            models.WatchSession.is_valid.is_(True),
            models.WatchSession.ended_at.isnot(None),
        )
    )

    if since is not None:
        query = query.filter(
            models.WatchSession.started_at >= since
        )

    watch_seconds, sessions_counted = query.one()

    return schemas.WatchPeriodStats(
        watch_seconds=int(watch_seconds),
        reels_watched=int(sessions_counted),
    )


@router.get(
    "/stats",
    response_model=schemas.WatchStatsResponse,
)
def get_watch_stats(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Return today's, last-7-days', last-30-days', and all-time
    valid watch statistics.
    """

    now = _now()

    today_start = now.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    week_start = now - timedelta(days=7)

    month_start = now - timedelta(days=30)

    return schemas.WatchStatsResponse(
        today=_period_stats(
            db,
            current_user.id,
            today_start,
        ),
        week=_period_stats(
            db,
            current_user.id,
            week_start,
        ),
        month=_period_stats(
            db,
            current_user.id,
            month_start,
        ),
        total=_period_stats(
            db,
            current_user.id,
            None,
        ),
    )