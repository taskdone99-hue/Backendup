"""Creator-facing Reel insights and playback analytics event ingestion."""
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db

router = APIRouter(tags=["Reel Insights"])


class ReelInsightEventIn(BaseModel):
    event_type: Literal["view", "progress", "skip"]
    source: Literal["profile", "feed", "reels_tab", "explore", "stories"] | None = None
    watched_seconds: float | None = Field(default=None, ge=0, le=86400)
    progress_percent: float | None = Field(default=None, ge=0, le=100)
    # Generate a UUID per client event and retry with the same value on network errors.
    client_event_id: str | None = Field(default=None, min_length=1, max_length=100)


def _owned_reel(db: Session, reel_id: int, user: models.User) -> models.Reel:
    reel = db.query(models.Reel).filter(models.Reel.id == reel_id).first()
    if reel is None:
        raise HTTPException(status_code=404, detail="Reel not found")
    if reel.user_id != user.id:
        raise HTTPException(status_code=403, detail="Only the Reel owner can view insights")
    return reel


def _events(db: Session, reel_id: int, audience: str | None = None):
    query = db.query(models.ReelInsightEvent).filter(models.ReelInsightEvent.reel_id == reel_id)
    if audience == "followers":
        query = query.filter(models.ReelInsightEvent.audience_type == "followers")
    elif audience == "non_followers":
        query = query.filter(models.ReelInsightEvent.audience_type == "non_followers")
    return query


def _count(db: Session, model, *filters) -> int:
    return int(db.query(func.count(model.id)).filter(*filters).scalar() or 0)


@router.post("/api/reels/{reel_id}/insights/events", status_code=status.HTTP_201_CREATED)
def record_reel_insight_event(
    reel_id: int,
    data: ReelInsightEventIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Record playback events. The Reel owner cannot inflate their own insights."""
    reel = db.query(models.Reel).filter(models.Reel.id == reel_id).first()
    if reel is None:
        raise HTTPException(status_code=404, detail="Reel not found")
    if reel.user_id == user.id:
        raise HTTPException(status_code=400, detail="Creator playback is not counted in insights")

    follows = db.query(models.Follow.id).filter(
        models.Follow.follower_id == user.id,
        models.Follow.following_id == reel.user_id,
    ).first()
    event = models.ReelInsightEvent(
        reel_id=reel_id,
        viewer_id=user.id,
        event_type=data.event_type,
        audience_type="followers" if follows else "non_followers",
        source=data.source,
        watched_seconds=data.watched_seconds,
        progress_percent=data.progress_percent,
        client_event_id=data.client_event_id,
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # A duplicate client_event_id is an idempotent retry, not a second view.
        if data.client_event_id:
            existing = db.query(models.ReelInsightEvent.id).filter(
                models.ReelInsightEvent.reel_id == reel_id,
                models.ReelInsightEvent.viewer_id == user.id,
                models.ReelInsightEvent.client_event_id == data.client_event_id,
            ).first()
            if existing:
                return {"recorded": False, "duplicate": True}
        raise HTTPException(status_code=409, detail="Could not record analytics event")
    db.refresh(event)
    return {"recorded": True, "event_id": event.id, "audience": event.audience_type}


@router.get("/api/reels/{reel_id}/insights")
def reel_insights_overview(
    reel_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    reel = _owned_reel(db, reel_id, user)
    view_q = _events(db, reel_id).filter(models.ReelInsightEvent.event_type == "view")
    views = int(view_q.count())
    reach = int(view_q.with_entities(func.count(func.distinct(models.ReelInsightEvent.viewer_id))).scalar() or 0)
    sessions = db.query(models.WatchSession).filter(
        models.WatchSession.reel_id == reel_id,
        models.WatchSession.user_id != reel.user_id,
        models.WatchSession.is_valid.is_(True),
        models.WatchSession.watch_seconds.isnot(None),
    ).all()
    total_watch_seconds = sum(int(s.watch_seconds or 0) for s in sessions)
    session_count = len(sessions)
    like_count = _count(db, models.Like, models.Like.target_type == models.LikeTargetType.reel, models.Like.target_id == reel_id)
    comment_count = _count(db, models.Comment, models.Comment.reel_id == reel_id)
    share_count = _count(db, models.Share, models.Share.content_type == models.ShareContentType.reel, models.Share.content_id == reel_id)
    save_count = _count(db, models.SavedItem, models.SavedItem.target_type == models.SavedItemType.reel, models.SavedItem.target_id == reel_id)
    return {
        "reel_id": reel_id,
        "views": views,
        "reach": reach,
        "total_watch_time_seconds": total_watch_seconds,
        "average_watch_time_seconds": round(total_watch_seconds / session_count, 2) if session_count else None,
        "followers_gained": None,
        "revenue": None,
        "engagement": {
            "likes": like_count,
            "comments": comment_count,
            "shares": share_count,
            "saves": save_count,
            "reposts": None,
        },
        "availability": {
            "followers_gained": "unavailable_without_reel_attribution",
            "revenue": "unavailable_without_reel_earnings_ledger",
            "reposts": "unavailable_without_repost_tracking",
            "views": "available_from_insight_events",
            "watch_time": "available_from_valid_watch_sessions",
        },
        "created_at": reel.created_at.isoformat() if reel.created_at else None,
    }


@router.get("/api/reels/{reel_id}/insights/timeline")
def reel_insights_timeline(
    reel_id: int,
    audience: Literal["all", "followers", "non_followers"] = Query("all"),
    days: int = Query(7, ge=1, le=90),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _owned_reel(db, reel_id, user)
    start = datetime.now(timezone.utc) - timedelta(days=days - 1)
    query = _events(db, reel_id, None if audience == "all" else audience).filter(
        models.ReelInsightEvent.event_type == "view",
        models.ReelInsightEvent.created_at >= start,
    ).order_by(models.ReelInsightEvent.created_at.asc())
    daily: dict[str, int] = {}
    for event in query.all():
        created = event.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        key = created.date().isoformat()
        daily[key] = daily.get(key, 0) + 1
    points = []
    for offset in range(days):
        day = (start.date() + timedelta(days=offset)).isoformat()
        points.append({"date": day, "views": daily.get(day, 0)})
    return {"reel_id": reel_id, "audience": audience, "days": days, "data": points}


@router.get("/api/reels/{reel_id}/insights/impact-rates")
def reel_insights_impact_rates(reel_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    _owned_reel(db, reel_id, user)
    views = _count(db, models.ReelInsightEvent, models.ReelInsightEvent.reel_id == reel_id, models.ReelInsightEvent.event_type == "view")
    def rate(count):
        return round((count / views) * 100, 2) if views else None
    likes = _count(db, models.Like, models.Like.target_type == models.LikeTargetType.reel, models.Like.target_id == reel_id)
    shares = _count(db, models.Share, models.Share.content_type == models.ShareContentType.reel, models.Share.content_id == reel_id)
    saves = _count(db, models.SavedItem, models.SavedItem.target_type == models.SavedItemType.reel, models.SavedItem.target_id == reel_id)
    comments = _count(db, models.Comment, models.Comment.reel_id == reel_id)
    skips = _count(db, models.ReelInsightEvent, models.ReelInsightEvent.reel_id == reel_id, models.ReelInsightEvent.event_type == "skip")
    return {"reel_id": reel_id, "denominator_views": views, "rates_percent": {
        "skip": rate(skips), "share": rate(shares), "like": rate(likes), "save": rate(saves),
        "repost": None, "comment": rate(comments)},
        "unavailable": {"repost": "repost_events_not_tracked"}}


@router.get("/api/reels/{reel_id}/insights/retention")
def reel_insights_retention(reel_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    _owned_reel(db, reel_id, user)
    rows = _events(db, reel_id).filter(
        models.ReelInsightEvent.event_type == "progress",
        models.ReelInsightEvent.progress_percent.isnot(None),
    ).all()
    if not rows:
        return {"reel_id": reel_id, "available": False, "data": []}
    viewers_by_bucket: dict[int, set[int]] = {}
    for row in rows:
        bucket = min(100, int(float(row.progress_percent) // 5) * 5)
        viewers_by_bucket.setdefault(bucket, set()).add(row.viewer_id)
    denominator = int(_events(db, reel_id).filter(
        models.ReelInsightEvent.event_type == "view"
    ).with_entities(func.count(func.distinct(models.ReelInsightEvent.viewer_id))).scalar() or 0)
    data = [
        {"progress_percent": point, "retained_viewers": len(viewers),
         "retention_percent": round(len(viewers) * 100 / denominator, 2) if denominator else None}
        for point, viewers in sorted(viewers_by_bucket.items())
    ]
    return {"reel_id": reel_id, "available": bool(data), "data": data}


@router.get("/api/reels/{reel_id}/insights/sources")
def reel_insights_sources(reel_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    _owned_reel(db, reel_id, user)
    rows = _events(db, reel_id).filter(
        models.ReelInsightEvent.event_type == "view",
        models.ReelInsightEvent.source.isnot(None),
    ).all()
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.source] = counts.get(row.source, 0) + 1
    total = sum(counts.values())
    labels = ["profile", "feed", "reels_tab", "explore", "stories"]
    return {"reel_id": reel_id, "available": bool(total), "total_attributed_views": total,
            "data": [{"source": label, "views": counts.get(label, 0),
                      "percentage": round(counts.get(label, 0) * 100 / total, 2) if total else None}
                     for label in labels] if total else []}
