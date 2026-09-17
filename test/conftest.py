"""
Shared pytest fixtures.

The app normally talks to MySQL. Tests run against an in-memory SQLite
database instead, with `PRAGMA foreign_keys=ON` so SQLite enforces foreign
keys the same way MySQL/InnoDB does — without that pragma SQLite silently
allows dangling references, and the reel-delete regression these tests
cover (a foreign key violation blowing up as a 500) wouldn't reproduce.

app.database is repointed at the SQLite engine *before* app.main is
imported, because main.py calls Base.metadata.create_all(bind=engine) at
import time and would otherwise try to reach the real database.
"""

import os
import sys

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import database  # noqa: E402

_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,  # one shared connection, so :memory: survives between sessions
)


@event.listens_for(_engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


_TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

database.engine = _engine
database.SessionLocal = _TestingSessionLocal

from app import models  # noqa: E402
from app.auth import get_current_user, get_current_user_optional  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services import notification_service  # noqa: E402


class _NullWebSocketManager:
    """Stands in for the notifications WebSocket manager — no sockets in tests."""

    async def send_to_user(self, *args, **kwargs):
        return None


notification_service.notification_manager = _NullWebSocketManager()
notification_service.send_push = lambda *args, **kwargs: None


@pytest.fixture()
def db():
    """A fresh schema per test, so tests can't leak rows into each other."""
    Base.metadata.create_all(bind=_engine)
    session = _TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=_engine)


@pytest.fixture()
def client(db):
    """TestClient wired to the test session, with authentication stubbed.

    `client.login(user)` selects who the request is coming from; the real
    get_current_user does JWT work that's irrelevant to what's under test
    here, so it's overridden rather than exercised.
    """
    from fastapi.testclient import TestClient

    state = {"user": None}

    def _override_get_db():
        yield db

    def _override_get_current_user():
        if state["user"] is None:
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
            )
        return state["user"]

    def _override_get_current_user_optional():
        return state["user"]

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_get_current_user
    app.dependency_overrides[get_current_user_optional] = _override_get_current_user_optional

    test_client = TestClient(app)
    test_client.login = lambda user: state.__setitem__("user", user)
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def make_user(db):
    counter = {"n": 0}

    def _make(username=None, *, is_private=False, is_active=True):
        counter["n"] += 1
        username = username or f"user{counter['n']}"
        user = models.User(
            username=username,
            email=f"{username}@example.test",
            hashed_password="not-a-real-hash",
            is_active=is_active,
            is_private=is_private,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    return _make


@pytest.fixture()
def make_reel(db):
    def _make(owner, video_url="/static/reels/test.mp4", **kwargs):
        reel = models.Reel(user_id=owner.id, video_url=video_url, **kwargs)
        db.add(reel)
        db.commit()
        db.refresh(reel)
        return reel

    return _make


@pytest.fixture()
def follow(db):
    def _follow(follower, following):
        db.add(models.Follow(follower_id=follower.id, following_id=following.id))
        db.commit()

    return _follow
