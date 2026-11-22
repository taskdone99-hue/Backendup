"""Long-form Video API parity routes. Video data remains separate from Reels."""
from datetime import datetime, timedelta, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session
from app import models
from app.auth import get_current_user
from app.database import get_db
from app.routers.long_video_routes import _out

router = APIRouter(tags=["Videos", "Video Insights"])


def _video(db: Session, video_id: int):
    row = db.query(models.Video).filter(models.Video.id == video_id).first()
    if row is None:
        raise HTTPException(404, "Video not found")
    return row


def _owned(db: Session, video_id: int, user):
    row = _video(db, video_id)
    if row.user_id != user.id:
        raise HTTPException(403, "Only the Video owner can view or manage this resource")
    return row


def _public_clause(user_id: int):
    return (models.Video.visibility == "public") | (models.Video.user_id == user_id)


def _page(db, query, limit, offset):
    total = query.count()
    rows = query.order_by(models.Video.created_at.desc(), models.Video.id.desc()).offset(offset).limit(limit).all()
    return {"total": total, "limit": limit, "offset": offset, "items": [_out(v) for v in rows]}


@router.get("/api/videos/home")
def video_home(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
               db: Session = Depends(get_db), user=Depends(get_current_user)):
    q = db.query(models.Video).filter(_public_clause(user.id))
    return _page(db, q, limit, offset)


@router.get("/api/videos/feed")
def video_feed(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
               db: Session = Depends(get_db), user=Depends(get_current_user)):
    # Chronological public feed; private videos are visible only to their owner.
    q = db.query(models.Video).filter(_public_clause(user.id))
    return _page(db, q, limit, offset)


@router.get("/api/videos/trending")
def trending_videos(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                    db: Session = Depends(get_db), user=Depends(get_current_user)):
    likes = db.query(models.VideoLike.video_id, func.count(models.VideoLike.id).label("likes")).group_by(models.VideoLike.video_id).subquery()
    views = db.query(models.VideoInsightEvent.video_id, func.count(models.VideoInsightEvent.id).label("views")).filter(models.VideoInsightEvent.event_type == "view").group_by(models.VideoInsightEvent.video_id).subquery()
    q = db.query(models.Video).filter(_public_clause(user.id)).outerjoin(likes, likes.c.video_id == models.Video.id).outerjoin(views, views.c.video_id == models.Video.id)
    q = q.order_by((func.coalesce(likes.c.likes, 0) + func.coalesce(views.c.views, 0)).desc(), models.Video.created_at.desc())
    return _page(db, q, limit, offset)


@router.post("/api/videos/{video_id}/save", status_code=201)
def save_video(video_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    video = _video(db, video_id)
    if video.visibility != "public" and video.user_id != user.id:
        raise HTTPException(404, "Video not found")
    saved = db.query(models.VideoSavedItem).filter_by(video_id=video_id, user_id=user.id).first()
    if saved is None:
        saved = models.VideoSavedItem(video_id=video_id, user_id=user.id)
        db.add(saved); db.commit(); db.refresh(saved)
    count = db.query(models.VideoSavedItem).filter_by(video_id=video_id).count()
    return {"message": "Video saved", "video_id": video_id, "saved": True, "saves_count": count}


@router.delete("/api/videos/{video_id}/save")
def unsave_video(video_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _video(db, video_id)
    saved = db.query(models.VideoSavedItem).filter_by(video_id=video_id, user_id=user.id).first()
    if saved:
        db.delete(saved); db.commit()
    count = db.query(models.VideoSavedItem).filter_by(video_id=video_id).count()
    return {"message": "Video unsaved", "video_id": video_id, "saved": False, "saves_count": count}


class VideoTagIn(BaseModel):
    user_id: int = Field(gt=0)
    x_position: float | None = Field(default=None, ge=0, le=1)
    y_position: float | None = Field(default=None, ge=0, le=1)


@router.post("/api/videos/{video_id}/tags", status_code=201)
def add_video_tag(video_id: int, payload: VideoTagIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _owned(db, video_id, user)
    target = db.query(models.User).filter(models.User.id == payload.user_id).first()
    if target is None:
        raise HTTPException(404, "User to tag not found")
    row = db.query(models.VideoTag).filter_by(video_id=video_id, user_id=payload.user_id).first()
    if row is None:
        row = models.VideoTag(video_id=video_id, user_id=payload.user_id)
        db.add(row)
    row.x_position, row.y_position = payload.x_position, payload.y_position
    db.commit(); db.refresh(row)
    return {"id": row.id, "video_id": video_id, "user_id": row.user_id, "x_position": row.x_position, "y_position": row.y_position, "tagged_at": row.tagged_at}


@router.get("/api/videos/{video_id}/tags")
def list_video_tags(video_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _video(db, video_id)
    rows = db.query(models.VideoTag).filter_by(video_id=video_id).order_by(models.VideoTag.tagged_at.asc()).all()
    return {"total": len(rows), "items": [{"id": r.id, "video_id": r.video_id, "user_id": r.user_id, "x_position": r.x_position, "y_position": r.y_position, "tagged_at": r.tagged_at} for r in rows]}


@router.delete("/api/videos/{video_id}/tags/{user_id}")
def remove_video_tag(video_id: int, user_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _owned(db, video_id, user)
    row = db.query(models.VideoTag).filter_by(video_id=video_id, user_id=user_id).first()
    if row is None:
        raise HTTPException(404, "Video tag not found")
    db.delete(row); db.commit()
    return {"message": "Tag removed", "video_id": video_id, "user_id": user_id}


class VideoRemixIn(BaseModel):
    audio_id: int = Field(gt=0)


@router.post("/api/videos/{video_id}/audio-remix", status_code=201)
def create_video_remix(video_id: int, payload: VideoRemixIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _video(db, video_id)
    audio = db.query(models.Audio).filter(models.Audio.id == payload.audio_id).first()
    if audio is None:
        raise HTTPException(404, "Audio not found")
    row = db.query(models.VideoAudioRemix).filter_by(video_id=video_id, user_id=user.id).first()
    if row is None:
        row = models.VideoAudioRemix(video_id=video_id, user_id=user.id, audio_id=payload.audio_id)
        db.add(row)
    else:
        row.audio_id = payload.audio_id
    db.commit(); db.refresh(row)
    return {"id": row.id, "video_id": video_id, "user_id": user.id, "audio_id": row.audio_id, "created_at": row.created_at}


@router.get("/api/videos/{video_id}/remixes")
def list_video_remixes(video_id: int, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                       db: Session = Depends(get_db), user=Depends(get_current_user)):
    _video(db, video_id)
    q = db.query(models.VideoAudioRemix).filter_by(video_id=video_id)
    total = q.count()
    rows = q.order_by(models.VideoAudioRemix.created_at.desc()).offset(offset).limit(limit).all()
    return {"total": total, "limit": limit, "offset": offset, "items": [{"id": r.id, "video_id": r.video_id, "user_id": r.user_id, "audio_id": r.audio_id, "created_at": r.created_at} for r in rows]}


class VideoInsightIn(BaseModel):
    event_type: Literal["view", "progress", "skip"]
    source: Literal["profile", "feed", "videos_tab", "explore", "search", "suggested_content", "other"] | None = None
    watched_seconds: float | None = Field(default=None, ge=0, le=86400)
    progress_percent: float | None = Field(default=None, ge=0, le=100)
    client_event_id: str | None = Field(default=None, min_length=1, max_length=100)


def _events(db, video_id):
    return db.query(models.VideoInsightEvent).filter(models.VideoInsightEvent.video_id == video_id)


def _period_start(period: str):
    now = datetime.now(timezone.utc)
    delta = {"12h": timedelta(hours=12), "24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30), "6m": timedelta(days=183), "1y": timedelta(days=365)}.get(period)
    return now - delta if delta else None


@router.post("/api/videos/{video_id}/insights/events", status_code=201)
def record_video_insight(video_id: int, payload: VideoInsightIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    video = _video(db, video_id)
    if video.user_id == user.id:
        raise HTTPException(400, "Creator playback is not counted in insights")
    row = models.VideoInsightEvent(video_id=video_id, viewer_id=user.id, event_type=payload.event_type, source=payload.source,
                                   watched_seconds=payload.watched_seconds, progress_percent=payload.progress_percent,
                                   client_event_id=payload.client_event_id)
    db.add(row)
    try:
        db.commit()
    except Exception:
        db.rollback()
        if payload.client_event_id and _events(db, video_id).filter_by(viewer_id=user.id, client_event_id=payload.client_event_id).first():
            return {"recorded": False, "duplicate": True}
        raise
    db.refresh(row)
    return {"recorded": True, "event_id": row.id, "video_id": video_id}


def _insight_data(db, video_id, period):
    start = _period_start(period)
    q = _events(db, video_id)
    if start:
        q = q.filter(models.VideoInsightEvent.created_at >= start)
    rows = q.all()
    views = [r for r in rows if r.event_type == "view"]
    watch = [r.watched_seconds for r in views if r.watched_seconds is not None]
    reach = len({r.viewer_id for r in views})
    likes = db.query(models.VideoLike).filter_by(video_id=video_id).count()
    comments = db.query(models.VideoComment).filter_by(video_id=video_id).count()
    saves = db.query(models.VideoSavedItem).filter_by(video_id=video_id).count()
    return {"video_id": video_id, "period": period, "period_start": start.isoformat() if start else None,
            "views": len(views), "reach": reach, "total_watch_time_seconds": round(sum(watch), 2),
            "average_watch_time_seconds": round(sum(watch)/len(watch), 2) if watch else None,
            "watch_time_events": len(watch), "engagement": {"likes": likes, "comments": comments, "shares": None, "saves": saves, "reposts": None},
            "availability": {"shares": "not_tracked_for_videos", "reposts": "not_tracked_for_videos", "followers_gained": "not_attributed", "revenue": "no_video_earnings_ledger"}}


@router.get("/api/videos/{video_id}/insights")
def video_insights(video_id: int, period: Literal["12h","24h","7d","30d","6m","1y","lifetime"] = "lifetime",
                   db: Session = Depends(get_db), user=Depends(get_current_user)):
    _owned(db, video_id, user)
    return _insight_data(db, video_id, period)


@router.get("/api/videos/{video_id}/insights/engagement")
def video_insight_engagement(video_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _owned(db, video_id, user)
    d = _insight_data(db, video_id, "lifetime")
    e = d["engagement"]; total = e["likes"] + e["comments"] + e["saves"]
    return {"video_id": video_id, "total_interactions": total, **e, "shares": None, "reposts": None}


@router.get("/api/videos/{video_id}/insights/reach")
def video_insight_reach(video_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _owned(db, video_id, user); d = _insight_data(db, video_id, "lifetime")
    return {"video_id": video_id, "total_reach": d["reach"], "unique_accounts_reached": d["reach"], "total_views": d["views"]}


@router.get("/api/videos/{video_id}/insights/views")
def video_insight_views(video_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _owned(db, video_id, user); d = _insight_data(db, video_id, "lifetime")
    return {"video_id": video_id, "total_views": d["views"], "period": "lifetime"}


@router.get("/api/videos/{video_id}/insights/interactions")
def video_insight_interactions(video_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _owned(db, video_id, user); d = _insight_data(db, video_id, "lifetime")
    return {"video_id": video_id, "engagement": d["engagement"], "availability": d["availability"]}


@router.get("/api/videos/{video_id}/insights/timeline")
def video_insight_timeline(video_id: int, period: Literal["12h","24h","7d","30d","6m","1y","lifetime"] = "30d",
                           db: Session = Depends(get_db), user=Depends(get_current_user)):
    _owned(db, video_id, user); start = _period_start(period)
    q = _events(db, video_id).filter(models.VideoInsightEvent.event_type == "view")
    if start: q = q.filter(models.VideoInsightEvent.created_at >= start)
    rows = q.order_by(models.VideoInsightEvent.created_at.asc()).all()
    buckets = {}
    for r in rows:
        key = r.created_at.date().isoformat() if r.created_at else "unknown"
        buckets[key] = buckets.get(key, 0) + 1
    return {"video_id": video_id, "period": period, "items": [{"date": k, "views": v} for k,v in sorted(buckets.items())]}


@router.get("/api/videos/{video_id}/insights/impact-rates")
def video_insight_impact_rates(video_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _owned(db, video_id, user); d = _insight_data(db, video_id, "lifetime")
    return {"video_id": video_id, "views": d["views"], "reach": d["reach"], "engagement_rate": None,
            "availability": "engagement_rate_not_calculated_without_impression_denominator"}


@router.get("/api/videos/{video_id}/insights/retention")
def video_insight_retention(video_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _owned(db, video_id, user)
    rows = _events(db, video_id).filter(models.VideoInsightEvent.progress_percent.isnot(None)).all()
    return {"video_id": video_id, "average_progress_percent": round(sum(r.progress_percent for r in rows)/len(rows),2) if rows else None,
            "samples": len(rows), "items": [{"progress_percent": r.progress_percent, "watched_seconds": r.watched_seconds, "created_at": r.created_at} for r in rows[-100:]]}


@router.get("/api/videos/{video_id}/insights/sources")
def video_insight_sources(video_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _owned(db, video_id, user)
    rows = _events(db, video_id).filter(models.VideoInsightEvent.event_type == "view").all()
    counts = {}
    for r in rows: counts[r.source or "unknown"] = counts.get(r.source or "unknown", 0) + 1
    return {"video_id": video_id, "total_views": len(rows), "items": [{"source": k, "views": v} for k,v in sorted(counts.items())]}


@router.get("/api/videos/{video_id}/audience")
def video_audience(video_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _owned(db, video_id, user)
    return {"video_id": video_id, "unique_viewers": _events(db, video_id).filter_by(event_type="view").with_entities(func.count(func.distinct(models.VideoInsightEvent.viewer_id))).scalar() or 0,
            "demographics": {"age": None, "gender": None, "countries": None},
            "availability": {"age": "not_collected", "gender": "not_collected", "countries": "not_collected"}}


@router.get("/api/videos/{video_id}/audience/viewers")
def video_audience_viewers(video_id: int, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                           db: Session = Depends(get_db), user=Depends(get_current_user)):
    _owned(db, video_id, user)
    q = _events(db, video_id).filter_by(event_type="view")
    total = q.with_entities(func.count(func.distinct(models.VideoInsightEvent.viewer_id))).scalar() or 0
    rows = q.order_by(models.VideoInsightEvent.created_at.desc()).offset(offset).limit(limit).all()
    seen = set(); items = []
    for r in rows:
        if r.viewer_id not in seen:
            seen.add(r.viewer_id); items.append({"user_id": r.viewer_id, "last_viewed_at": r.created_at})
    return {"video_id": video_id, "total": total, "limit": limit, "offset": offset, "items": items}


@router.get("/api/videos/{video_id}/audience/age")
def video_audience_age(video_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _owned(db, video_id, user); return {"video_id": video_id, "available": False, "reason": "Viewer age demographics are not collected"}


@router.get("/api/videos/{video_id}/audience/countries")
def video_audience_countries(video_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _owned(db, video_id, user); return {"video_id": video_id, "available": False, "reason": "Viewer country analytics are not collected"}


@router.get("/api/videos/{video_id}/audience/gender")
def video_audience_gender(video_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _owned(db, video_id, user); return {"video_id": video_id, "available": False, "reason": "Viewer gender analytics are not collected"}
