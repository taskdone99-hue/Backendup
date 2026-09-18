"""
Reel watch-time monetization eligibility.

Reuses the existing watch-session tracking in app/routers/watch_routes.py
(WatchSession model + its _owner_period_stats helper) rather than building
a second watch-time counting system — server-timestamped, server-validated
WatchSession rows are the only source of truth here; nothing client-
supplied (e.g. a "watched_seconds" field) is ever read directly.

Attribution model: watch time is credited to the Reel *owner*, not the
viewer. When someone watches user X's Reel, that valid watch time counts
toward X's monetization — not toward the viewer's own monetization. A user
watching their own Reel contributes 0 seconds, no matter how long the
session, since self-views can't be used to inflate a creator's own
eligibility. See _owner_period_stats() in app.routers.watch_routes for the
exact query (join on Reel.user_id, exclude WatchSession.user_id ==
owner_id, require is_valid=True + ended_at is not null).

Configuration (env vars, not hard-coded, not duplicated across files):
  MONETIZATION_REQUIRED_WATCH_SECONDS  default 7200 (2 hours)
  MONETIZATION_QUALIFICATION_PERIOD    "lifetime" (default) | "daily" | "weekly"

There's no existing creator-monetization or ad-revenue eligibility flag
anywhere in this project (checked models.User and the membership/payment
routers) — REEL_MONETIZATION_REQUIRED_SECONDS is a new, standalone concept
("is this creator eligible for monetization based on watch time earned
from other viewers"), so nothing here touches
MembershipPlan/UserMembership/PaymentOrder. Status is computed dynamically
from WatchSession on every call rather than persisted on the user row,
specifically to avoid the "stale state after a period reset" problem the
task calls out — there is nothing to go stale.
"""

import os

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models, schemas
from app.routers.watch_routes import _owner_period_stats

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
    """
    Monetization eligibility for `user_id` as a Reel *owner*: valid watch
    time other viewers have spent on their Reels (their own views of their
    own Reels never count — see _owner_period_stats).
    """
    since = _qualification_window_start(MONETIZATION_QUALIFICATION_PERIOD)
    stats = _owner_period_stats(db, user_id, since)

    watch_time_seconds = stats.watch_seconds
    required = REEL_MONETIZATION_REQUIRED_SECONDS
    remaining = max(0, required - watch_time_seconds)

    return schemas.MonetizationStatusOut(
        monetization_enabled=watch_time_seconds >= required,
        watch_time_seconds=watch_time_seconds,
        required_watch_time_seconds=required,
        remaining_seconds=remaining,
    )


# --------------------------------------------------------------------------
# Creator earnings ledger
#
# CreatorEarning rows are only ever inserted by the system in response to a
# real event — a brand collaboration or creator-collaboration request being
# accepted (see app.services.brand_collaboration_service /
# app.services.collaboration_service). There is no endpoint here that lets
# a client credit itself an arbitrary amount.
# --------------------------------------------------------------------------

DEFAULT_EARNINGS_CURRENCY = os.getenv("DEFAULT_EARNINGS_CURRENCY", "INR")


def record_earning(
    db: Session,
    *,
    user_id: int,
    source_type: models.EarningSourceType,
    amount_cents: int,
    source_id: int | None = None,
    currency: str = DEFAULT_EARNINGS_CURRENCY,
    description: str | None = None,
) -> models.CreatorEarning:
    """Insert one ledger row. Caller is responsible for its own commit if it
    needs to bundle this with other changes in the same transaction; this
    function itself does not commit so it can be called as part of a larger
    accept-request flow without a partial commit in between."""
    entry = models.CreatorEarning(
        user_id=user_id,
        source_type=source_type,
        source_id=source_id,
        amount_cents=amount_cents,
        currency=currency,
        description=description,
    )
    db.add(entry)
    db.flush()
    return entry


def list_earnings(
    db: Session,
    user_id: int,
    *,
    source_type: models.EarningSourceType | None = None,
    limit: int = 20,
    offset: int = 0,
):
    query = db.query(models.CreatorEarning).filter(models.CreatorEarning.user_id == user_id)
    if source_type is not None:
        query = query.filter(models.CreatorEarning.source_type == source_type)

    total = query.count()
    total_cents = (
        db.query(func.coalesce(func.sum(models.CreatorEarning.amount_cents), 0))
        .filter(models.CreatorEarning.user_id == user_id)
        .scalar()
    )
    rows = (
        query.order_by(models.CreatorEarning.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return total, int(total_cents), rows


def get_earnings_summary(db: Session, user_id: int) -> schemas.CreatorEarningsSummaryOut:
    rows = (
        db.query(
            models.CreatorEarning.source_type,
            func.coalesce(func.sum(models.CreatorEarning.amount_cents), 0),
        )
        .filter(models.CreatorEarning.user_id == user_id)
        .group_by(models.CreatorEarning.source_type)
        .all()
    )
    by_source = {source.value: int(amount) for source, amount in rows}
    total_cents = sum(by_source.values())

    return schemas.CreatorEarningsSummaryOut(
        total_earnings_cents=total_cents,
        currency=DEFAULT_EARNINGS_CURRENCY,
        by_source=by_source,
        monetization=get_monetization_status(db, user_id),
    )
