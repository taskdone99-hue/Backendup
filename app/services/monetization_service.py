"""
Reel watch-time monetization eligibility.

Reuses the existing watch-session tracking in app/routers/watch_routes.py
(WatchSession model + its _period_stats helper) rather than building a
second watch-time counting system — the same query that powers
GET /api/watch/stats already sums only *valid* (>= MIN_VALID_WATCH_SECONDS,
ended) sessions for a user over a given period, which is exactly what
"prevent duplicate/double counting" and "don't trust client-provided watch
time" require: this never reads anything the client submits directly, only
server-timestamped WatchSession rows.

Configuration (env vars, not hard-coded, not duplicated across files):
  MONETIZATION_REQUIRED_WATCH_SECONDS  default 7200 (2 hours)
  MONETIZATION_QUALIFICATION_PERIOD    "lifetime" (default) | "daily" | "weekly"

There's no existing creator-monetization or ad-revenue eligibility flag
anywhere in this project (checked models.User and the membership/payment
routers) — REEL_MONETIZATION_REQUIRED_SECONDS is a new, standalone concept
("is this creator eligible for monetization based on watch time"), so
nothing here touches MembershipPlan/UserMembership/PaymentOrder. Status is
computed dynamically from WatchSession on every call rather than persisted
on the user row, specifically to avoid the "stale state after a period
reset" problem the task calls out — there is nothing to go stale.
"""

import os

from sqlalchemy.orm import Session

from app import schemas
from app.routers.watch_routes import _period_stats

REEL_MONETIZATION_REQUIRED_SECONDS = int(
    os.getenv("MONETIZATION_REQUIRED_WATCH_SECONDS", "7200")
)

# "lifetime": all-time valid watch time (the default — matches "watch Reels
# for a total of 2 hours" with no existing daily/session model to match).
# "daily"/"weekly": rolling window from now, for when that requirement
# changes later — see module docstring.
MONETIZATION_QUALIFICATION_PERIOD = os.getenv(
    "MONETIZATION_QUALIFICATION_PERIOD", "lifetime"
).lower()


def _qualification_window_start(period: str):
    from datetime import datetime, timedelta, timezone

    if period == "daily":
        now = datetime.now(timezone.utc)
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "weekly":
        return datetime.now(timezone.utc) - timedelta(days=7)
    return None  # "lifetime"


def get_monetization_status(db: Session, user_id: int) -> schemas.MonetizationStatusOut:
    since = _qualification_window_start(MONETIZATION_QUALIFICATION_PERIOD)
    stats = _period_stats(db, user_id, since)

    watch_time_seconds = stats.watch_seconds
    required = REEL_MONETIZATION_REQUIRED_SECONDS
    remaining = max(0, required - watch_time_seconds)

    return schemas.MonetizationStatusOut(
        monetization_enabled=watch_time_seconds >= required,
        watch_time_seconds=watch_time_seconds,
        required_watch_time_seconds=required,
        remaining_seconds=remaining,
    )
