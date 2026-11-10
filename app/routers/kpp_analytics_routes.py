"""KPP Partner Program and creator analytics dashboard APIs."""
import os
from collections import Counter
from datetime import datetime, timedelta, timezone, date, time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db
from app.routers.watch_routes import _owner_period_stats

router = APIRouter(tags=["KPP Partner Program", "Creator Analytics"])

FOLLOWERS_REQUIRED = int(os.getenv("KPP_REQUIRED_FOLLOWERS", "500"))
WATCH_HOURS_REQUIRED = float(os.getenv("KPP_REQUIRED_WATCH_HOURS", "500"))
ACCOUNT_DAYS_REQUIRED = int(os.getenv("KPP_REQUIRED_ACCOUNT_AGE_DAYS", "3"))
UPLOAD_DAYS_REQUIRED = 3
POLICY_VERSION = os.getenv("KPP_POLICY_VERSION", "2026-11-v1")
PRIZE_LABEL = os.getenv("KPP_ANNUAL_PRIZE_LABEL", "₹1 Crore")
PRIZE_AMOUNT = int(os.getenv("KPP_ANNUAL_PRIZE_AMOUNT_INR", "10000000"))
EARNING_METHODS = [
    {"key": "fans_subscription", "title": "Fans Subscription"},
    {"key": "brand_promotions", "title": "Brand Promotions"},
    {"key": "commissions", "title": "Earn Commissions"},
    {"key": "donations", "title": "Donations"},
]


def _now():
    return datetime.now(timezone.utc)


def _utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _upload_dates(db: Session, user_id: int, start_day: date, end_day: date) -> set[date]:
    start = datetime.combine(start_day, time.min, tzinfo=timezone.utc)
    end = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=timezone.utc)
    days = set()
    reel_times = db.query(models.Reel.created_at).filter(
        models.Reel.user_id == user_id, models.Reel.created_at >= start, models.Reel.created_at < end
    ).all()
    for (created_at,) in reel_times:
        if created_at:
            days.add(_utc(created_at).date())
    post_times = db.query(models.Post.created_at).filter(
        models.Post.user_id == user_id, models.Post.media_type == models.MediaType.video,
        models.Post.created_at >= start, models.Post.created_at < end
    ).all()
    for (created_at,) in post_times:
        if created_at:
            days.add(_utc(created_at).date())
    for (created_at,) in db.query(models.Post.created_at).join(
        models.PostMedia, models.PostMedia.post_id == models.Post.id
    ).filter(models.Post.user_id == user_id, models.PostMedia.media_type == models.MediaType.video,
             models.Post.created_at >= start, models.Post.created_at < end).distinct().all():
        if created_at:
            days.add(_utc(created_at).date())
    return days


def _eligibility(db: Session, user: models.User) -> dict:
    now = _now()
    created = _utc(user.created_at)
    age_days = max(0, (now - created).days) if created else 0
    followers = int(db.query(func.count(models.Follow.id)).filter(models.Follow.following_id == user.id).scalar() or 0)
    watch_seconds = _owner_period_stats(db, user.id, None).watch_seconds
    watch_hours = round(watch_seconds / 3600, 2)
    required_days = [now.date() - timedelta(days=i) for i in (2, 1, 0)]
    uploaded = _upload_dates(db, user.id, required_days[0], required_days[-1])
    upload_count = sum(day in uploaded for day in required_days)
    standing_ok = bool(user.is_active and not user.is_suspended)
    email_ok = bool(user.email and user.is_email_verified)
    phone_ok = bool(user.phone_number and user.is_phone_verified)

    checks = [
        ("followers", "Followers", followers, FOLLOWERS_REQUIRED, "followers", followers >= FOLLOWERS_REQUIRED),
        ("watch_time", "Watch Time", watch_hours, WATCH_HOURS_REQUIRED, "hours", watch_hours >= WATCH_HOURS_REQUIRED),
        ("account_age", "Account Age", age_days, ACCOUNT_DAYS_REQUIRED, "days", age_days >= ACCOUNT_DAYS_REQUIRED),
        ("recent_uploads", "Recent Uploads", upload_count, UPLOAD_DAYS_REQUIRED, "consecutive_days", upload_count == UPLOAD_DAYS_REQUIRED),
    ]
    requirements = []
    for key, title, current, required, unit, passed in checks:
        requirements.append({"key": key, "title": title, "current": current, "required": required,
                             "unit": unit, "status": "verified" if passed else "pending"})
    requirements.extend([
        {"key": "account_standing", "title": "Account Standing", "current": "good" if standing_ok else "restricted",
         "required": "active_without_suspension", "status": "verified" if standing_ok else "pending",
         "basis": "users.is_active and users.is_suspended; this codebase has no separate active-violation registry"},
        {"key": "verification", "title": "Email & Mobile Verification",
         "current": {"email": email_ok, "mobile": phone_ok}, "required": {"email": True, "mobile": True},
         "status": "verified" if email_ok and phone_ok else "pending"},
    ])
    requirements[3]["period"] = "last_3_utc_calendar_days"
    requirements[3]["qualifying_dates"] = [d.isoformat() for d in required_days if d in uploaded]
    requirements[3]["rule"] = "At least one video or reel on each of the last 3 consecutive UTC calendar days"
    verified = sum(item["status"] == "verified" for item in requirements)
    return {"eligible": verified == 6, "verified_count": verified, "total_requirements": 6,
            "policy_version": POLICY_VERSION, "evaluated_at": now.isoformat(), "requirements": requirements}


@router.get("/api/v1/kpp/eligibility/requirements")
def kpp_requirements(user: models.User = Depends(get_current_user)):
    return {"policy_version": POLICY_VERSION, "requirements": [
        {"key": "followers", "required": FOLLOWERS_REQUIRED, "unit": "followers"},
        {"key": "watch_time", "required": WATCH_HOURS_REQUIRED, "unit": "hours"},
        {"key": "account_age", "required": ACCOUNT_DAYS_REQUIRED, "unit": "days"},
        {"key": "recent_uploads", "required": 3, "unit": "consecutive_days", "rule": "At least one video or reel on each of the last 3 consecutive UTC calendar days"},
        {"key": "account_standing", "required": "active_without_suspension"},
        {"key": "verification", "required": {"email": True, "mobile": True}},
    ]}


@router.get("/api/v1/kpp/eligibility")
def get_kpp_eligibility(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return _eligibility(db, user)


@router.post("/api/v1/kpp/eligibility/evaluate")
def evaluate_kpp_eligibility(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return _eligibility(db, user)


@router.get("/api/v1/kpp/earning-methods")
def kpp_earning_methods(user: models.User = Depends(get_current_user)):
    return {"items": EARNING_METHODS}


@router.get("/api/v1/kpp/overview")
def kpp_overview(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    eligibility = _eligibility(db, user)
    by_key = {item["key"]: item for item in eligibility["requirements"]}
    application = db.query(models.KPPApplication).filter(models.KPPApplication.user_id == user.id).first()
    return {
        "program": {"name": "KPP Partner Program", "annual_grand_prize": PRIZE_LABEL,
                    "annual_grand_prize_amount_inr": PRIZE_AMOUNT,
                    "award_title": "Top Content Creator of the Year",
                    "winner_frequency": "one_annual_winner_across_all_content_types"},
        "eligibility": eligibility,
        "progress": {key: {"current": by_key[key]["current"], "required": by_key[key]["required"], "unit": by_key[key].get("unit")}
                     for key in ("followers", "watch_time", "account_age", "recent_uploads")},
        "earning_methods": EARNING_METHODS,
        "application": None if application is None else {"id": application.id, "status": application.status,
            "submitted_at": application.submitted_at.isoformat() if application.submitted_at else None},
    }


@router.post("/api/v1/kpp/applications", status_code=status.HTTP_201_CREATED)
def submit_kpp_application(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    eligibility = _eligibility(db, user)  # Always re-evaluate on the server at submission time.
    if not eligibility["eligible"]:
        raise HTTPException(status_code=409, detail={"message": "KPP eligibility requirements are not yet met", "eligibility": eligibility})
    if db.query(models.KPPApplication.id).filter(models.KPPApplication.user_id == user.id).first():
        raise HTTPException(status_code=409, detail="A KPP application has already been submitted")
    row = models.KPPApplication(user_id=user.id, status="submitted", eligibility_snapshot=eligibility)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="A KPP application has already been submitted")
    db.refresh(row)
    return {"message": "KPP application submitted successfully", "application": {
        "id": row.id, "status": row.status, "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None}}


@router.get("/api/v1/kpp/applications/current")
def current_kpp_application(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    row = db.query(models.KPPApplication).filter(models.KPPApplication.user_id == user.id).first()
    if row is None:
        return {"application": None}
    return {"application": {"id": row.id, "status": row.status,
        "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None}}


class KPPApplicationStatusUpdate(BaseModel):
    status: Literal["under_review", "approved", "rejected"]


@router.patch("/api/v1/kpp/applications/{application_id}/status")
def update_kpp_application_status(application_id: int, payload: KPPApplicationStatusUpdate,
                                 db: Session = Depends(get_db), admin: models.User = Depends(get_current_user)):
    if not admin.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required")
    row = db.query(models.KPPApplication).filter(models.KPPApplication.id == application_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="KPP application not found")
    row.status = payload.status
    db.commit()
    db.refresh(row)
    return {"message": "KPP application status updated", "application": {
        "id": row.id, "user_id": row.user_id, "status": row.status,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None}}


PERIODS = {"7d": 7, "30d": 30, "90d": 90, "365d": 365}


def _views(db, user_id, start, end):
    return db.query(models.ReelInsightEvent).join(models.Reel, models.Reel.id == models.ReelInsightEvent.reel_id).filter(
        models.Reel.user_id == user_id, models.ReelInsightEvent.event_type == "view",
        models.ReelInsightEvent.created_at >= start, models.ReelInsightEvent.created_at < end)


def _change(current, previous):
    return None if previous == 0 else round((current - previous) * 100.0 / previous, 2)


def _owner_watch_seconds(db: Session, user_id: int, start: datetime, end: datetime) -> int:
    value = db.query(func.coalesce(func.sum(models.WatchSession.watch_seconds), 0)).join(
        models.Reel, models.Reel.id == models.WatchSession.reel_id
    ).filter(
        models.Reel.user_id == user_id,
        models.WatchSession.user_id != user_id,
        models.WatchSession.is_valid.is_(True),
        models.WatchSession.ended_at.isnot(None),
        models.WatchSession.started_at >= start,
        models.WatchSession.started_at < end,
    ).scalar()
    return int(value or 0)


@router.get("/api/v1/analytics/dashboard")
def analytics_dashboard(period: str = Query("30d", pattern="^(7d|30d|90d|365d)$"),
                        db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    now = _now()
    days = PERIODS[period]
    start, previous_start = now - timedelta(days=days), now - timedelta(days=days * 2)
    current, previous = _views(db, user.id, start, now), _views(db, user.id, previous_start, start)
    views = int(current.count())
    unique = int(current.with_entities(func.count(func.distinct(models.ReelInsightEvent.viewer_id))).scalar() or 0)
    # Watch time uses server-validated completed WatchSession rows, not client-reported durations.
    watch = _owner_watch_seconds(db, user.id, start, now)
    prev_views = int(previous.count())
    prev_unique = int(previous.with_entities(func.count(func.distinct(models.ReelInsightEvent.viewer_id))).scalar() or 0)
    prev_watch = _owner_watch_seconds(db, user.id, previous_start, start)
    followers = int(db.query(func.count(models.Follow.id)).filter(models.Follow.following_id == user.id).scalar() or 0)
    new_followers = int(db.query(func.count(models.Follow.id)).filter(models.Follow.following_id == user.id,
        models.Follow.created_at >= start, models.Follow.created_at < now).scalar() or 0)
    prev_followers = int(db.query(func.count(models.Follow.id)).filter(models.Follow.following_id == user.id,
        models.Follow.created_at >= previous_start, models.Follow.created_at < start).scalar() or 0)

    earnings = db.query(models.CreatorEarning.currency, func.coalesce(func.sum(models.CreatorEarning.amount_cents), 0)).filter(
        models.CreatorEarning.user_id == user.id, models.CreatorEarning.created_at >= start,
        models.CreatorEarning.created_at < now).group_by(models.CreatorEarning.currency).all()
    earnings_items = [{"currency": currency, "amount": round(float(amount) / 100, 2)} for currency, amount in earnings]

    # Last 48 hours, bucketed hourly (zero buckets are included for chart stability).
    rt_start = now - timedelta(hours=48)
    rt_rows = _views(db, user.id, rt_start, now).with_entities(models.ReelInsightEvent.created_at).all()
    hourly = Counter()
    for (created_at,) in rt_rows:
        dt = _utc(created_at)
        if dt:
            bucket = dt.replace(minute=0, second=0, microsecond=0)
            hourly[bucket.isoformat()] += 1
    rt_timeline = []
    bucket = rt_start.replace(minute=0, second=0, microsecond=0)
    while bucket <= now.replace(minute=0, second=0, microsecond=0):
        rt_timeline.append({"timestamp": bucket.isoformat(), "views": hourly[bucket.isoformat()]})
        bucket += timedelta(hours=1)

    daily_rows = current.with_entities(models.ReelInsightEvent.created_at).all()
    daily = Counter()
    for (created_at,) in daily_rows:
        dt = _utc(created_at)
        if dt:
            daily[dt.date().isoformat()] += 1
    timeline = [{"date": (now - timedelta(days=i)).date().isoformat(),
                 "views": daily[(now - timedelta(days=i)).date().isoformat()]}
                for i in range(days - 1, -1, -1)]
    watch_hours, previous_hours = round(watch / 3600, 2), round(prev_watch / 3600, 2)
    return {
        "period": period, "period_start": start.isoformat(), "generated_at": now.isoformat(),
        "profile": {"user_id": user.id, "username": user.username, "display_name": user.full_name},
        "metrics": {
            "total_views": {"value": views, "change_percent": _change(views, prev_views), "source": "reel_insight_events"},
            "unique_views": {"value": unique, "change_percent": _change(unique, prev_unique), "source": "distinct_reel_viewers"},
            "watch_time_hours": {"value": watch_hours, "change_percent": _change(watch_hours, previous_hours), "source": "validated_watch_sessions"},
            "followers": {"value": followers, "new_in_period": new_followers, "change_percent": _change(new_followers, prev_followers), "source": "follows"},
            "estimated_revenue": {"value": None, "currency": None, "change_percent": None,
                                  "availability": "unavailable_admob_does_not_send_revenue_to_backend"},
            "creator_earnings": {"items": earnings_items, "source": "creator_earnings_ledger"},
        },
        "realtime_views": {"window_hours": 48, "total": sum(hourly.values()), "timeline": rt_timeline},
        "views_timeline": timeline,
        "data_scope": {"views": "Reel playback insight events only; standard post views are not currently tracked in a unified event source",
                       "unique_views": "Distinct viewer accounts across the creator's Reels in the selected period",
                       "watch_time": "Sum of valid, completed watch sessions attributed to the creator's Reels; self-views excluded",
                       "revenue": "AdMob revenue is unavailable because the backend stores impressions and clicks only"},
    }
