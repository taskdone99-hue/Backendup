"""
Regression tests for the watch-time bug: a WatchSession that never got a
timely /watch/end (app killed, backgrounded, lost connectivity) was, before
this fix, auto-closed on the user's *next* /watch/start using "now" as the
end time — crediting the entire elapsed gap (hours or days) as watch time,
with no upper bound. A 10-second reel could show ~93,000 seconds
(~26 hours) of "watch time" from exactly one such stale session; this
wasn't duplicate/overlapping-session double counting (the DB-level
UniqueConstraint on active_owner_id already prevents that) — it was a
missing sanity check on a single session's duration.

Fix: app/routers/watch_routes.py's _close_session now also flags a session
invalid if its duration exceeds MAX_VALID_WATCH_SECONDS, the same way it
already flagged sessions under MIN_VALID_WATCH_SECONDS.

Test harness notes: same pattern as test/test_creator_monetization.py —
mounts the real watch_routes/monetization_routes routers on a private
app + private in-memory SQLite database.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["SECRET_KEY"] = "test-secret-key-for-watch-session-cap-tests"
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
from app.routers.watch_routes import router as watch_router, MAX_VALID_WATCH_SECONDS, MIN_VALID_WATCH_SECONDS
from app.routers.monetization_routes import router as monetization_router
from app.fix_stale_watch_sessions import fix_stale_watch_sessions

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


def _make_reel(user_id: int) -> int:
    db = TestSessionLocal()
    r = models.Reel(user_id=user_id, video_url="/static/reels/x.mp4")
    db.add(r)
    db.commit()
    db.refresh(r)
    rid = r.id
    db.close()
    return rid


def _backdate_active_session(user_id: int, hours_ago: float) -> None:
    """Simulates an already-open (started, not yet ended) session that's
    been sitting for `hours_ago` hours — e.g. the app was killed right
    after /watch/start with no /watch/end ever sent."""
    db = TestSessionLocal()
    session = (
        db.query(models.WatchSession)
        .filter(models.WatchSession.active_owner_id == user_id)
        .first()
    )
    session.started_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    db.merge(session)
    db.commit()
    db.close()


# ---- the core bug: a stale session auto-closed on the next /watch/start ----

def test_stale_abandoned_session_does_not_inflate_watch_time():
    # Reels are owned by someone else so this test actually exercises
    # "a stale session must not inflate the *owner's* monetization" rather
    # than trivially passing because of the (separate) self-view exclusion.
    owner = _make_user("watchcap_u1_owner")
    uid = _make_user("watchcap_u1")
    reel_a = _make_reel(owner)
    reel_b = _make_reel(owner)

    # User starts watching reel_a, then the app is killed — no /watch/end
    # ever arrives. Backdate it to simulate that session having been open
    # for ~26 hours (matches the reported ~92,985-second bug exactly).
    client.post("/api/watch/start", headers=_auth_headers(uid), json={"reel_id": reel_a})
    _backdate_active_session(uid, hours_ago=25.83)

    # Next time the user opens the app, they start watching reel_b — this
    # auto-closes the stale reel_a session using "now".
    resp = client.post("/api/watch/start", headers=_auth_headers(uid), json={"reel_id": reel_b})
    assert resp.status_code == 201

    db = TestSessionLocal()
    stale_session = (
        db.query(models.WatchSession)
        .filter(models.WatchSession.reel_id == reel_a, models.WatchSession.user_id == uid)
        .first()
    )
    assert stale_session.watch_seconds > MAX_VALID_WATCH_SECONDS
    assert stale_session.is_valid is False
    db.close()

    # And it must not count toward the Reel owner's monetization eligibility.
    status_resp = client.get("/api/monetization/status", headers=_auth_headers(owner))
    assert status_resp.json()["watch_time_seconds"] == 0


def test_normal_session_within_range_still_counts():
    # Owner and viewer are two different accounts: watch time is credited
    # to the Reel *owner*'s monetization, never to the viewer who generated
    # it, so this needs both to check that attribution correctly.
    owner = _make_user("watchcap_u2_owner")
    viewer = _make_user("watchcap_u2_viewer")
    reel = _make_reel(owner)

    start = client.post("/api/watch/start", headers=_auth_headers(viewer), json={"reel_id": reel}).json()
    _backdate_active_session(viewer, hours_ago=30 / 3600)  # 30 seconds ago — a real, brief watch
    end = client.post(
        "/api/watch/end", headers=_auth_headers(viewer), json={"session_id": start["session_id"]}
    )
    assert end.status_code == 200

    db = TestSessionLocal()
    session = db.query(models.WatchSession).filter(models.WatchSession.id == start["session_id"]).first()
    assert session.is_valid is True
    assert MIN_VALID_WATCH_SECONDS <= session.watch_seconds <= MAX_VALID_WATCH_SECONDS
    db.close()

    owner_status = client.get("/api/monetization/status", headers=_auth_headers(owner))
    assert owner_status.json()["watch_time_seconds"] >= 25

    # It must not also count toward the viewer's own monetization.
    viewer_status = client.get("/api/monetization/status", headers=_auth_headers(viewer))
    assert viewer_status.json()["watch_time_seconds"] == 0


def test_session_right_at_the_cap_boundary_is_valid():
    uid = _make_user("watchcap_u3")
    reel = _make_reel(uid)
    start = client.post("/api/watch/start", headers=_auth_headers(uid), json={"reel_id": reel}).json()
    _backdate_active_session(uid, hours_ago=MAX_VALID_WATCH_SECONDS / 3600)
    client.post("/api/watch/end", headers=_auth_headers(uid), json={"session_id": start["session_id"]})

    db = TestSessionLocal()
    session = db.query(models.WatchSession).filter(models.WatchSession.id == start["session_id"]).first()
    assert session.is_valid is True
    db.close()


def test_session_one_second_past_the_cap_is_invalid():
    uid = _make_user("watchcap_u4")
    reel = _make_reel(uid)
    start = client.post("/api/watch/start", headers=_auth_headers(uid), json={"reel_id": reel}).json()
    _backdate_active_session(uid, hours_ago=(MAX_VALID_WATCH_SECONDS + 1) / 3600)
    client.post("/api/watch/end", headers=_auth_headers(uid), json={"session_id": start["session_id"]})

    db = TestSessionLocal()
    session = db.query(models.WatchSession).filter(models.WatchSession.id == start["session_id"]).first()
    assert session.is_valid is False
    db.close()


# ---- data-repair script for rows already corrupted before this fix ----

def test_fix_stale_watch_sessions_repairs_preexisting_bad_rows():
    # Owner and viewer are two different accounts — see the note in
    # test_normal_session_within_range_still_counts above.
    owner = _make_user("watchcap_u5_owner")
    viewer = _make_user("watchcap_u5_viewer")
    reel = _make_reel(owner)

    db = TestSessionLocal()
    # Simulate a row written by the old, uncapped code: is_valid=True with
    # an absurd watch_seconds, exactly like the reported 92,985-second case.
    bad_session = models.WatchSession(
        user_id=viewer, reel_id=reel,
        started_at=datetime.now(timezone.utc) - timedelta(seconds=92985),
        ended_at=datetime.now(timezone.utc),
        watch_seconds=92985, active_owner_id=None, is_valid=True,
    )
    db.add(bad_session)
    db.commit()
    db.refresh(bad_session)
    bad_id = bad_session.id
    db.close()

    status_before = client.get("/api/monetization/status", headers=_auth_headers(owner)).json()
    assert status_before["watch_time_seconds"] == 92985

    db = TestSessionLocal()
    fixed_count = fix_stale_watch_sessions(db)
    db.close()
    assert fixed_count == 1

    status_after = client.get("/api/monetization/status", headers=_auth_headers(owner)).json()
    assert status_after["watch_time_seconds"] == 0

    db = TestSessionLocal()
    row = db.query(models.WatchSession).filter(models.WatchSession.id == bad_id).first()
    assert row.is_valid is False
    assert row.watch_seconds == 92985  # value kept for audit, just excluded via is_valid
    db.close()
