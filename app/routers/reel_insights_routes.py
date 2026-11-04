"""Creator-facing Reel insights and playback analytics event ingestion."""
import calendar
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
    source: Literal[
        "profile", "feed", "reels_tab", "explore", "stories",
        "suggested_content", "search", "other",
    ] | None = None
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


# The period values are shared by the overview (Views + Watch Time) and timeline
# (real-time Views + Watch Time) endpoints.
InsightsPeriod = Literal["12h", "24h", "7d", "30d", "6m", "1y", "lifetime"]


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _subtract_months(value: datetime, months: int) -> datetime:
    month_index = value.year * 12 + (value.month - 1) - months
    year, month_zero_based = divmod(month_index, 12)
    month = month_zero_based + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _period_start(period: InsightsPeriod, now: datetime) -> datetime | None:
    if period == "12h":
        return now - timedelta(hours=12)
    if period == "24h":
        return now - timedelta(hours=24)
    if period == "7d":
        return now - timedelta(days=7)
    if period == "30d":
        return now - timedelta(days=30)
    if period == "6m":
        return _subtract_months(now, 6)
    if period == "1y":
        return _subtract_months(now, 12)

    # Lifetime means every insight event recorded for this Reel. Do not use
    # reel.created_at as a lower bound: imported/backfilled events may predate
    # that timestamp, and database timestamp precision can otherwise exclude
    # events recorded at Reel creation time.
    return None


def _period_events(db: Session, reel_id: int, start: datetime | None):
    query = _events(db, reel_id)
    if start is not None:
        query = query.filter(models.ReelInsightEvent.created_at >= start)
    return query


def _timeline_bucket(value: datetime, granularity: str) -> datetime:
    value = _as_utc(value) or datetime.now(timezone.utc)
    if granularity == "hour":
        return value.replace(minute=0, second=0, microsecond=0)
    if granularity == "day":
        return value.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "week":
        day_start = value.replace(hour=0, minute=0, second=0, microsecond=0)
        return day_start - timedelta(days=day_start.weekday())
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_timeline_bucket(value: datetime, granularity: str) -> datetime:
    if granularity == "hour":
        return value + timedelta(hours=1)
    if granularity == "day":
        return value + timedelta(days=1)
    if granularity == "week":
        return value + timedelta(days=7)
    return _subtract_months(value.replace(day=1), -1)


def _timeline_label(value: datetime, granularity: str) -> str:
    if granularity == "hour":
        return value.strftime("%Y-%m-%dT%H:00:00Z")
    if granularity in ("day", "week"):
        return value.date().isoformat()
    return value.strftime("%Y-%m")


def _timeline_granularity(period: InsightsPeriod) -> str:
    if period in ("12h", "24h"):
        return "hour"
    if period in ("7d", "30d"):
        return "day"
    if period == "6m":
        return "week"
    return "month"


@router.get("/api/reels/{reel_id}/insights")
def reel_insights_overview(
    reel_id: int,
    period: InsightsPeriod = Query("lifetime"),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    reel = _owned_reel(db, reel_id, user)
    now = datetime.now(timezone.utc)
    start = _period_start(period, now)
    period_q = _period_events(db, reel_id, start)
    view_q = period_q.filter(models.ReelInsightEvent.event_type == "view")
    views = int(view_q.count())
    reach = int(view_q.with_entities(func.count(func.distinct(models.ReelInsightEvent.viewer_id))).scalar() or 0)

    # Reel Insights uses its own playback-event source. Do not read or mutate
    # WatchSession here: that table remains the source of truth for monetization.
    watch_q = view_q.filter(models.ReelInsightEvent.watched_seconds.isnot(None))
    total_watch_seconds = float(
        watch_q.with_entities(func.coalesce(func.sum(models.ReelInsightEvent.watched_seconds), 0)).scalar() or 0
    )
    watch_event_count = int(watch_q.with_entities(func.count(models.ReelInsightEvent.id)).scalar() or 0)
    total_watch_seconds = round(total_watch_seconds, 2)

    like_count = _count(db, models.Like, models.Like.target_type == models.LikeTargetType.reel, models.Like.target_id == reel_id)
    comment_count = _count(db, models.Comment, models.Comment.reel_id == reel_id)
    share_count = _count(db, models.Share, models.Share.content_type == models.ShareContentType.reel, models.Share.content_id == reel_id)
    save_count = _count(db, models.SavedItem, models.SavedItem.target_type == models.SavedItemType.reel, models.SavedItem.target_id == reel_id)
    return {
        "reel_id": reel_id,
        "period": period,
        "period_start": start.isoformat() if start else None,
        "views": views,
        "reach": reach,
        "total_watch_time_seconds": total_watch_seconds,
        "average_watch_time_seconds": round(total_watch_seconds / watch_event_count, 2) if watch_event_count else None,
        "watch_time_events": watch_event_count,
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
            "watch_time": "available_from_reel_insight_view_events",
        },
        "created_at": reel.created_at.isoformat() if reel.created_at else None,
    }




def _engagement_counts(db: Session, reel_id: int) -> dict:
    """Counts current persisted interactions; reposts remain unavailable until tracked."""
    likes = _count(
        db, models.Like,
        models.Like.target_type == models.LikeTargetType.reel,
        models.Like.target_id == reel_id,
    )
    comments = _count(db, models.Comment, models.Comment.reel_id == reel_id)
    shares = _count(
        db, models.Share,
        models.Share.content_type == models.ShareContentType.reel,
        models.Share.content_id == reel_id,
    )
    saves = _count(
        db, models.SavedItem,
        models.SavedItem.target_type == models.SavedItemType.reel,
        models.SavedItem.target_id == reel_id,
    )
    # There is no persisted Reel repost model/event in this project yet.
    total = likes + comments + shares + saves
    return {
        "total_interactions": total,
        "likes": likes,
        "comments": comments,
        "reposts": None,
        "shares": shares,
        "saves": saves,
        "availability": {"reposts": "unavailable_without_repost_tracking"},
    }


def _reach_counts(db: Session, reel_id: int) -> dict:
    view_events = _events(db, reel_id).filter(models.ReelInsightEvent.event_type == "view")
    unique_reach = int(
        view_events.with_entities(
            func.count(func.distinct(models.ReelInsightEvent.viewer_id))
        ).scalar() or 0
    )
    total_views = int(view_events.count())
    return {
        "total_reach": unique_reach,
        "unique_accounts_reached": unique_reach,
        "total_views": total_views,
    }


def _view_breakdown(db: Session, reel_id: int) -> dict:
    events = _events(db, reel_id)
    viewed = int(
        events.filter(models.ReelInsightEvent.event_type == "view")
        .with_entities(func.count(func.distinct(models.ReelInsightEvent.viewer_id)))
        .scalar() or 0
    )
    skipped = int(
        events.filter(models.ReelInsightEvent.event_type == "skip")
        .with_entities(func.count(func.distinct(models.ReelInsightEvent.viewer_id)))
        .scalar() or 0
    )
    # Only classify accounts when an explicit event was recorded. The two
    # categories may overlap if clients emit both events; no inference is made.
    classified = viewed + skipped
    return {
        "viewed": viewed,
        "skipped": skipped,
        "viewed_percentage": _percentage(viewed, classified),
        "skipped_percentage": _percentage(skipped, classified),
        "classified_accounts": classified,
        "available": classified > 0,
    }


def _source_breakdown(db: Session, reel_id: int) -> dict:
    rows = _events(db, reel_id).filter(
        models.ReelInsightEvent.event_type == "view",
        models.ReelInsightEvent.source.isnot(None),
    ).order_by(models.ReelInsightEvent.created_at.asc(), models.ReelInsightEvent.id.asc()).all()
    # Attribute each reached account to its first recorded discovery source so
    # source percentages are mutually exclusive and cannot exceed 100% in total.
    first_source_by_viewer: dict[int, str] = {}
    for event in rows:
        first_source_by_viewer.setdefault(event.viewer_id, event.source)
    counts: dict[str, int] = {}
    for source in first_source_by_viewer.values():
        counts[source] = counts.get(source, 0) + 1
    total = sum(counts.values())
    labels = [
        "suggested_content", "search", "feed", "profile",
        "reels_tab", "explore", "stories", "other",
    ]
    return {
        "available": total > 0,
        "total_attributed_accounts": total,
        "data": [
            {
                "source": label,
                "unique_accounts": counts.get(label, 0),
                "percentage": _percentage(counts.get(label, 0), total),
            }
            for label in labels
        ] if total else [],
    }


@router.get("/api/reels/{reel_id}/insights/engagement")
def reel_insights_engagement(
    reel_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Combined Engagement screen payload for the Reel owner."""
    _owned_reel(db, reel_id, user)
    reach = _reach_counts(db, reel_id)
    views = _view_breakdown(db, reel_id)
    sources = _source_breakdown(db, reel_id)
    interactions = _engagement_counts(db, reel_id)
    engagement_rate = _percentage(interactions["total_interactions"], reach["unique_accounts_reached"])
    return {
        "reel_id": reel_id,
        "reach": reach,
        "viewing": views,
        "reach_sources": sources,
        "interactions": interactions,
        "engagement_rate": engagement_rate,
        "engagement_rate_basis": "total_interactions / unique_accounts_reached * 100",
    }


@router.get("/api/reels/{reel_id}/insights/reach")
def reel_insights_reach(
    reel_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _owned_reel(db, reel_id, user)
    return {"reel_id": reel_id, **_reach_counts(db, reel_id)}


@router.get("/api/reels/{reel_id}/insights/views")
def reel_insights_views(
    reel_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _owned_reel(db, reel_id, user)
    return {"reel_id": reel_id, **_view_breakdown(db, reel_id)}


@router.get("/api/reels/{reel_id}/insights/interactions")
def reel_insights_interactions(
    reel_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _owned_reel(db, reel_id, user)
    return {"reel_id": reel_id, **_engagement_counts(db, reel_id)}


@router.get("/api/reels/{reel_id}/insights/timeline")
def reel_insights_timeline(
    reel_id: int,
    period: InsightsPeriod = Query("7d"),
    audience: Literal["all", "followers", "non_followers"] = Query("all"),
    # Keep the legacy days parameter working for existing clients. New clients
    # should use period so all seven filters are available consistently.
    days: int | None = Query(None, ge=1, le=90),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    reel = _owned_reel(db, reel_id, user)
    now = datetime.now(timezone.utc)

    if days is not None and period == "7d":
        start = now - timedelta(days=days)
        granularity = "day"
        selected_period = f"{days}d"
    else:
        start = _period_start(period, now)
        granularity = _timeline_granularity(period)
        selected_period = period

    query = _period_events(db, reel_id, start)
    if audience != "all":
        query = query.filter(models.ReelInsightEvent.audience_type == audience)
    query = query.filter(models.ReelInsightEvent.event_type == "view").order_by(
        models.ReelInsightEvent.created_at.asc()
    )

    totals: dict[datetime, dict[str, float | int]] = {}
    total_views = 0
    total_watch_time_seconds = 0.0
    for event in query.all():
        created = _as_utc(event.created_at)
        if created is None:
            continue
        watched_seconds = float(event.watched_seconds or 0)
        bucket = _timeline_bucket(created, granularity)
        point = totals.setdefault(bucket, {"views": 0, "watch_time_seconds": 0.0})
        point["views"] += 1
        point["watch_time_seconds"] += watched_seconds
        total_views += 1
        total_watch_time_seconds += watched_seconds

    first_bucket = _timeline_bucket(start, granularity) if start else None
    current_bucket = _timeline_bucket(now, granularity)
    if first_bucket is None:
        first_bucket = min(totals) if totals else current_bucket

    points = []
    bucket = first_bucket
    # A cap protects lifetime charts for very old Reels. The API still returns
    # lifetime totals in the overview; timeline output is bounded to 600 buckets.
    max_buckets = 600
    if granularity == "month" and (current_bucket.year - bucket.year) * 12 + current_bucket.month - bucket.month > max_buckets:
        bucket = _subtract_months(current_bucket, max_buckets - 1)
    while bucket <= current_bucket and len(points) < max_buckets:
        point = totals.get(bucket, {"views": 0, "watch_time_seconds": 0.0})
        points.append({
            "bucket": _timeline_label(bucket, granularity),
            "views": int(point["views"]),
            "watch_time_seconds": round(float(point["watch_time_seconds"]), 2),
        })
        bucket = _next_timeline_bucket(bucket, granularity)

    return {
        "reel_id": reel_id,
        "period": selected_period,
        "audience": audience,
        "granularity": granularity,
        "total_views": total_views,
        "total_watch_time_seconds": round(total_watch_time_seconds, 2),
        "data": points,
    }


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
        models.ReelInsightEvent.event_type.in_(("progress", "view")),
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
    breakdown = _source_breakdown(db, reel_id)
    # Keep legacy `views` field for existing clients while adding unique reach.
    data = []
    for item in breakdown["data"]:
        source = item["source"]
        event_count = _count(
            db, models.ReelInsightEvent,
            models.ReelInsightEvent.reel_id == reel_id,
            models.ReelInsightEvent.event_type == "view",
            models.ReelInsightEvent.source == source,
        )
        data.append({**item, "views": event_count})
    return {
        "reel_id": reel_id,
        "available": breakdown["available"],
        "total_attributed_views": sum(item["views"] for item in data),
        "total_attributed_accounts": breakdown["total_attributed_accounts"],
        "data": data,
    }


def _unique_viewers(db: Session, reel_id: int):
    """Return distinct users who generated a recorded view for this Reel."""
    viewer_ids = db.query(models.ReelInsightEvent.viewer_id).filter(
        models.ReelInsightEvent.reel_id == reel_id,
        models.ReelInsightEvent.event_type == "view",
    ).distinct().subquery()
    return db.query(models.User).join(
        viewer_ids, models.User.id == viewer_ids.c.viewer_id
    )


def _percentage(count: int, total: int):
    return round(count * 100 / total, 2) if total else None


def _age_on(dob, today):
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


@router.get("/api/reels/{reel_id}/audience")
def reel_audience_summary(
    reel_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _owned_reel(db, reel_id, user)
    viewers = _unique_viewers(db, reel_id).all()
    with_dob = sum(1 for viewer in viewers if viewer.date_of_birth is not None)
    with_gender = sum(
        1 for viewer in viewers
        if viewer.gender is not None and getattr(viewer.gender, "value", viewer.gender) != "prefer_not_to_say"
    )
    return {
        "reel_id": reel_id,
        "unique_viewers": len(viewers),
        "categories": {
            "age": {"available": with_dob > 0, "status": "available" if with_dob else "unavailable"},
            "country": {"available": False, "status": "unavailable", "reason": "viewer_country_not_collected"},
            "gender": {"available": with_gender > 0, "status": "available" if with_gender else "unavailable"},
        },
        "viewers": {"available": False, "status": "unavailable", "reason": "individual_viewer_identities_not_exposed"},
    }


@router.get("/api/reels/{reel_id}/audience/viewers")
def reel_audience_viewers_status(
    reel_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _owned_reel(db, reel_id, user)
    return {
        "reel_id": reel_id,
        "available": False,
        "status": "unavailable",
        "reason": "individual_viewer_identities_not_exposed",
    }


@router.get("/api/reels/{reel_id}/audience/age")
def reel_audience_age(
    reel_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _owned_reel(db, reel_id, user)
    today = datetime.now(timezone.utc).date()
    viewers = [v for v in _unique_viewers(db, reel_id).all() if v.date_of_birth is not None]
    groups = [("13-17", 13, 17), ("18-24", 18, 24), ("25-34", 25, 34),
              ("35-44", 35, 44), ("45-54", 45, 54), ("55-64", 55, 64), ("65+", 65, None)]
    counts = {label: 0 for label, _, _ in groups}
    for viewer in viewers:
        age = _age_on(viewer.date_of_birth, today)
        for label, lower, upper in groups:
            if age >= lower and (upper is None or age <= upper):
                counts[label] += 1
                break
    total = len(viewers)
    return {
        "reel_id": reel_id, "available": total > 0,
        "status": "available" if total else "unavailable",
        "denominator_viewers": total,
        "data": [
            {"age_group": label, "viewers": counts[label], "percentage": _percentage(counts[label], total)}
            for label, _, _ in groups
        ] if total else [],
    }


@router.get("/api/reels/{reel_id}/audience/countries")
def reel_audience_countries(
    reel_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _owned_reel(db, reel_id, user)
    return {
        "reel_id": reel_id, "available": False, "status": "unavailable",
        "reason": "viewer_country_not_collected", "denominator_viewers": 0, "data": [],
    }


@router.get("/api/reels/{reel_id}/audience/gender")
def reel_audience_gender(
    reel_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _owned_reel(db, reel_id, user)
    viewers = _unique_viewers(db, reel_id).all()
    labels = ("male", "female", "non_binary")
    counts = {label: 0 for label in labels}
    for viewer in viewers:
        value = getattr(viewer.gender, "value", viewer.gender)
        if value in counts:
            counts[value] += 1
    total = sum(counts.values())
    return {
        "reel_id": reel_id, "available": total > 0,
        "status": "available" if total else "unavailable",
        "denominator_viewers": total,
        "data": [
            {"gender": label, "viewers": counts[label], "percentage": _percentage(counts[label], total)}
            for label in labels
        ] if total else [],
    }
