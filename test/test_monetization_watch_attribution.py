"""
Tests for monetization watch-time *attribution*: valid watch time on a
Reel is credited to the Reel's owner, never to the viewer who generated
it, and a Reel owner watching their own Reel never counts toward their
own eligibility.

Flow under test:
  Viewer watches Reel
    -> WatchSession created via POST /api/watch/start
    -> valid watch_seconds calculated server-side via POST /api/watch/end
       (never trusts a client-supplied duration -- see watch_routes.py)
    -> reel.user_id (the owner) is looked up
    -> viewer_id == owner_id  -> 0 seconds credited
    -> viewer_id != owner_id  -> watch_seconds credited to the owner's
       GET /api/monetization/status, checked against the 7200-second
       (2-hour) monetization threshold

This reuses the existing WatchSession system end to end (real
/watch/start, /watch/end, and /monetization/status endpoints) rather than
re-implementing watch-time math in the test, and reuses
app.routers.watch_routes._owner_period_stats via
app.services.monetization_service.get_monetization_status.

Test harness notes: same pattern as test/test_watch_session_caps.py and
test/test_creator_monetization.py -- mounts the real watch_routes /
monetization_routes routers on a private app + private in-memory SQLite
database, so this exercises the actual endpoint/service/schema code over
HTTP exactly as app.main would wire it, without fighting other test
files over the shared app.main.app singleton.

Run with:
    pip install -r requirements.txt pytest
    pytest test/test_monetization_watch_attribution.py -v
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["SECRET_KEY"] = "test-secret-key-for-monetization-attribution-tests"
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("DB_USER", "test")
os.environ.setdefault("DB_PASSWORD", "test")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.exceptions import HTTPException as StarletteHTTPException

import app.database as database
import app.auth as auth

auth.SECRET_KEY = os.environ["SECRET_KEY"]

test_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
database.engine = test_engine
database.SessionLocal = TestSessionLocal

from app import models
from app.database import Base, get_db
from app.auth import create_access_token
from app.routers.watch_routes import (
    router as watch_router,
    MIN_VALID_WATCH_SECONDS,
    MAX_VALID_WATCH_SECONDS,
)
from app.routers.monetization_routes import router as monetization_router
from app.services.monetization_service import REEL_MONETIZATION_REQUIRED_SECONDS

Base.metadata.create_all(bind=test_engine)

test_app = FastAPI()
test_app.include_router(watch_router)
test_app.include_router(monetization_router)


@test_app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    first = exc.errors()[0]
    msg = first["msg"]
    prefix = "Value error, "
    if msg.startswith(prefix):
        msg = msg[len(prefix):]
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"message": msg})


@test_app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code, content={"message": exc.detail},
        headers=getattr(exc, "headers", None),
    )


def override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


test_app.dependency_overrides[get_db] = override_get_db
client = TestClient(test_app)


def _auth_headers(user_id: int) -> dict:
    token = create_access_token({"sub": str(user_id)})
    return {"Authorization": f"Bearer {token}"}


def _make_user(username: str) -> int:
    db = TestSessionLocal()
    u = models.User(username=username, full_name=username, is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    uid = u.id
    db.close()
    return uid


def _make_reel(owner_id: int) -> int:
    db = TestSessionLocal()
    r = models.Reel(user_id=owner_id, video_url="/static/reels/attr.mp4")
    db.add(r)
    db.commit()
    db.refresh(r)
    rid = r.id
    db.close()
    return rid


def _monetization_status(user_id: int) -> dict:
    resp = client.get("/api/monetization/status", headers=_auth_headers(user_id))
    assert resp.status_code == 200
    return resp.json()


def _watch(viewer_id: int, reel_id: int, seconds: float) -> dict:
    """
    Drive a real, complete watch session through the actual /watch/start
    and /watch/end endpoints (never writes watch_seconds directly), then
    backdate the session's started_at so the server-computed elapsed time
    comes out to approximately `seconds`. This is the same trick used in
    test/test_watch_session_caps.py.
    """
    start = client.post(
        "/api/watch/start", headers=_auth_headers(viewer_id), json={"reel_id": reel_id}
    ).json()

    db = TestSessionLocal()
    session = (
        db.query(models.WatchSession)
        .filter(models.WatchSession.active_owner_id == viewer_id)
        .first()
    )
    session.started_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    db.merge(session)
    db.commit()
    db.close()

    end = client.post(
        "/api/watch/end",
        headers=_auth_headers(viewer_id),
        json={"session_id": start["session_id"]},
    )
    assert end.status_code == 200
    return end.json()


# ---------------------------------------------------------------------
# Own Reel -> 0 seconds
# ---------------------------------------------------------------------

def test_watching_own_reel_counts_zero_toward_own_monetization():
    owner = _make_user("attr_self")
    reel = _make_reel(owner)

    result = _watch(owner, reel, seconds=50)
    assert result["counted"] is True  # a normal, valid session...

    # ...but it must not count toward the owner's own monetization, since
    # viewer_id == owner_id.
    body = _monetization_status(owner)
    assert body["watch_time_seconds"] == 0
    assert body["monetization_enabled"] is False


def test_watching_own_reel_repeatedly_still_counts_zero():
    owner = _make_user("attr_self_repeat")
    reel = _make_reel(owner)

    for _ in range(5):
        _watch(owner, reel, seconds=100)

    body = _monetization_status(owner)
    assert body["watch_time_seconds"] == 0


# ---------------------------------------------------------------------
# Other user watches -> time goes to the Reel owner, not the viewer
# ---------------------------------------------------------------------

def test_other_users_watch_time_credits_the_reel_owner():
    owner = _make_user("attr_owner_a")
    viewer = _make_user("attr_viewer_a")
    reel = _make_reel(owner)

    _watch(viewer, reel, seconds=120)

    owner_status = _monetization_status(owner)
    assert owner_status["watch_time_seconds"] == 120

    # It must not also show up on the viewer's own monetization status.
    viewer_status = _monetization_status(viewer)
    assert viewer_status["watch_time_seconds"] == 0


def test_watch_time_does_not_leak_to_an_unrelated_owner():
    owner_a = _make_user("attr_owner_b1")
    owner_b = _make_user("attr_owner_b2")
    viewer = _make_user("attr_viewer_b")
    reel_a = _make_reel(owner_a)

    _watch(viewer, reel_a, seconds=200)

    assert _monetization_status(owner_a)["watch_time_seconds"] == 200
    # owner_b has no Reels watched at all -- must stay at 0.
    assert _monetization_status(owner_b)["watch_time_seconds"] == 0


# ---------------------------------------------------------------------
# Multiple viewers accumulate onto the same owner
# ---------------------------------------------------------------------

def test_multiple_viewers_accumulate_for_the_same_owner():
    owner = _make_user("attr_owner_c")
    viewer1 = _make_user("attr_viewer_c1")
    viewer2 = _make_user("attr_viewer_c2")
    viewer3 = _make_user("attr_viewer_c3")
    reel = _make_reel(owner)

    _watch(viewer1, reel, seconds=100)
    _watch(viewer2, reel, seconds=150)
    _watch(viewer3, reel, seconds=75)

    body = _monetization_status(owner)
    assert body["watch_time_seconds"] == 325


def test_multiple_reels_from_the_same_owner_accumulate():
    owner = _make_user("attr_owner_d")
    viewer = _make_user("attr_viewer_d")
    reel_1 = _make_reel(owner)
    reel_2 = _make_reel(owner)

    _watch(viewer, reel_1, seconds=100)
    _watch(viewer, reel_2, seconds=250)

    body = _monetization_status(owner)
    assert body["watch_time_seconds"] == 350


# ---------------------------------------------------------------------
# 7200 seconds -> eligible
# ---------------------------------------------------------------------

def test_exactly_7200_seconds_makes_owner_eligible():
    assert REEL_MONETIZATION_REQUIRED_SECONDS == 7200

    owner = _make_user("attr_owner_e")
    viewer = _make_user("attr_viewer_e")
    reel = _make_reel(owner)

    # MAX_VALID_WATCH_SECONDS caps a single session, so reach 7200 across
    # several distinct, valid sessions -- exactly how a real viewer would
    # generate it over multiple watches.
    per_session = MAX_VALID_WATCH_SECONDS
    remaining = 7200
    while remaining > 0:
        chunk = min(per_session, remaining)
        _watch(viewer, reel, seconds=chunk)
        remaining -= chunk

    body = _monetization_status(owner)
    assert body["watch_time_seconds"] == 7200
    assert body["monetization_enabled"] is True
    assert body["remaining_seconds"] == 0


def test_just_under_7200_seconds_is_not_yet_eligible():
    owner = _make_user("attr_owner_f")
    viewer = _make_user("attr_viewer_f")
    reel = _make_reel(owner)

    per_session = MAX_VALID_WATCH_SECONDS
    remaining = 7199
    while remaining > 0:
        chunk = min(per_session, remaining)
        _watch(viewer, reel, seconds=chunk)
        remaining -= chunk

    body = _monetization_status(owner)
    assert body["watch_time_seconds"] == 7199
    assert body["monetization_enabled"] is False
    assert body["remaining_seconds"] == 1


# ---------------------------------------------------------------------
# Invalid / duplicate sessions -> not counted
# ---------------------------------------------------------------------

def test_too_short_session_is_not_counted():
    owner = _make_user("attr_owner_g")
    viewer = _make_user("attr_viewer_g")
    reel = _make_reel(owner)

    # Below MIN_VALID_WATCH_SECONDS -- a stray tap, not a real watch.
    result = _watch(viewer, reel, seconds=max(0, MIN_VALID_WATCH_SECONDS - 1))
    assert result["counted"] is False

    body = _monetization_status(owner)
    assert body["watch_time_seconds"] == 0


def test_stale_abandoned_session_is_not_counted_toward_owner():
    owner = _make_user("attr_owner_h")
    viewer = _make_user("attr_viewer_h")
    reel_a = _make_reel(owner)
    reel_b = _make_reel(owner)

    # Viewer starts watching reel_a, app is killed -- no /watch/end ever
    # arrives. Backdate the still-open session to simulate it sitting
    # abandoned for ~26 hours.
    client.post("/api/watch/start", headers=_auth_headers(viewer), json={"reel_id": reel_a})
    db = TestSessionLocal()
    stale = (
        db.query(models.WatchSession)
        .filter(models.WatchSession.active_owner_id == viewer)
        .first()
    )
    stale.started_at = datetime.now(timezone.utc) - timedelta(hours=25.83)
    db.merge(stale)
    db.commit()
    db.close()

    # Viewer starts watching reel_b -- this auto-closes the stale reel_a
    # session using "now", producing a huge elapsed duration that must be
    # flagged invalid rather than credited to the owner.
    resp = client.post("/api/watch/start", headers=_auth_headers(viewer), json={"reel_id": reel_b})
    assert resp.status_code == 201

    db = TestSessionLocal()
    stale_row = (
        db.query(models.WatchSession)
        .filter(models.WatchSession.reel_id == reel_a, models.WatchSession.user_id == viewer)
        .first()
    )
    assert stale_row.watch_seconds > MAX_VALID_WATCH_SECONDS
    assert stale_row.is_valid is False
    db.close()

    body = _monetization_status(owner)
    assert body["watch_time_seconds"] == 0


def test_duplicate_legitimate_sessions_sum_without_double_counting():
    """
    'Duplicate' here means two separate, real watch sessions from the same
    viewer on the same Reel -- they must sum normally (not double-bill a
    single session, and not get deduplicated away either).
    """
    owner = _make_user("attr_owner_i")
    viewer = _make_user("attr_viewer_i")
    reel = _make_reel(owner)

    _watch(viewer, reel, seconds=40)
    _watch(viewer, reel, seconds=60)

    body = _monetization_status(owner)
    assert body["watch_time_seconds"] == 100


def test_a_second_watch_start_cannot_credit_the_same_session_twice():
    """
    The DB-level UniqueConstraint on active_owner_id means a viewer can
    only ever have one *open* session; calling /watch/start again while
    one is open auto-closes the old one first (via the same _close_session
    validity check) instead of allowing two open sessions to somehow both
    get credited for overlapping time.
    """
    owner = _make_user("attr_owner_j")
    viewer = _make_user("attr_viewer_j")
    reel = _make_reel(owner)

    first_start = client.post(
        "/api/watch/start", headers=_auth_headers(viewer), json={"reel_id": reel}
    ).json()

    db = TestSessionLocal()
    session = (
        db.query(models.WatchSession)
        .filter(models.WatchSession.id == first_start["session_id"])
        .first()
    )
    session.started_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    db.merge(session)
    db.commit()
    db.close()

    # No /watch/end sent -- immediately start a second session. This must
    # auto-close the first one (crediting ~30s) rather than leaving it
    # open/uncounted or double-crediting it.
    second_start = client.post(
        "/api/watch/start", headers=_auth_headers(viewer), json={"reel_id": reel}
    )
    assert second_start.status_code == 201

    db = TestSessionLocal()
    first_session = (
        db.query(models.WatchSession)
        .filter(models.WatchSession.id == first_start["session_id"])
        .first()
    )
    assert first_session.ended_at is not None
    assert first_session.is_valid is True
    db.close()

    # End the second session too, then confirm the total is exactly the
    # sum of the two real sessions, not more.
    end = client.post(
        "/api/watch/end",
        headers=_auth_headers(viewer),
        json={"session_id": second_start.json()["session_id"]},
    )
    assert end.status_code == 200

    body = _monetization_status(owner)
    expected = first_session.watch_seconds + end.json()["watch_seconds"]
    assert body["watch_time_seconds"] == expected


# ---------------------------------------------------------------------
# Unauthenticated / sanity
# ---------------------------------------------------------------------

def test_monetization_status_requires_auth():
    resp = client.get("/api/monetization/status")
    assert resp.status_code in (401, 403)
