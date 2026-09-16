"""
Tests for Privacy: block / unblock, restrict / unrestrict, mute / unmute
(posts + stories toggles), and their wiring into feeds/comments.

Same harness pattern as test/test_hashtags_and_media.py — private app +
private in-memory SQLite, mounting the real, unmodified routers.
"""

import os
import sys
from pathlib import Path

os.environ["SECRET_KEY"] = "test-secret-key-for-privacy-tests"
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
from app.routers.privacy_routes import router as privacy_router
from app.routers.content_routes import router as posts_router
from app.routers.comment_routes import router as comments_router

Base.metadata.create_all(bind=test_engine)

test_app = FastAPI()
test_app.include_router(privacy_router)
test_app.include_router(posts_router)
test_app.include_router(comments_router)


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


def _fake_image(name: str = "a.jpg") -> tuple:
    return (name, b"\xff\xd8\xff\xe0fakejpegbytes", "image/jpeg")


@pytest.fixture(scope="module")
def users():
    db = TestSessionLocal()
    a = models.User(username="privacy_a", full_name="A", is_active=True)
    b = models.User(username="privacy_b", full_name="B", is_active=True)
    db.add_all([a, b])
    db.commit()
    db.refresh(a)
    db.refresh(b)
    ids = (a.id, b.id)
    db.close()
    return ids


# ---- block ----

def test_block_then_unblock_user(users):
    a_id, b_id = users
    resp = client.post(f"/api/privacy/block/{b_id}", headers=_auth_headers(a_id))
    assert resp.status_code == 200
    assert resp.json()["is_blocked"] is True

    listed = client.get("/api/privacy/blocked", headers=_auth_headers(a_id)).json()
    assert any(u["id"] == b_id for u in listed["items"])

    resp = client.delete(f"/api/privacy/block/{b_id}", headers=_auth_headers(a_id))
    assert resp.status_code == 200
    assert resp.json()["is_blocked"] is False

    listed = client.get("/api/privacy/blocked", headers=_auth_headers(a_id)).json()
    assert all(u["id"] != b_id for u in listed["items"])


def test_cannot_block_self(users):
    a_id, _ = users
    resp = client.post(f"/api/privacy/block/{a_id}", headers=_auth_headers(a_id))
    assert resp.status_code == 400


def test_blocked_user_post_hidden_from_blocker(users):
    a_id, b_id = users
    post = client.post(
        "/api/posts", headers=_auth_headers(b_id),
        data={"caption": "hello from b"}, files={"file": _fake_image()},
    ).json()

    client.post(f"/api/privacy/block/{b_id}", headers=_auth_headers(a_id))

    resp = client.get(f"/api/posts/{post['id']}", headers=_auth_headers(a_id))
    assert resp.status_code == 404

    client.delete(f"/api/privacy/block/{b_id}", headers=_auth_headers(a_id))
    resp = client.get(f"/api/posts/{post['id']}", headers=_auth_headers(a_id))
    assert resp.status_code == 200


def test_blocked_user_cannot_comment_on_your_post(users):
    a_id, b_id = users
    post = client.post(
        "/api/posts", headers=_auth_headers(a_id),
        data={"caption": "a's post"}, files={"file": _fake_image()},
    ).json()
    client.post(f"/api/privacy/block/{b_id}", headers=_auth_headers(a_id))

    resp = client.post(
        f"/api/posts/{post['id']}/comments", headers=_auth_headers(b_id), json={"content": "hey"}
    )
    assert resp.status_code == 403
    client.delete(f"/api/privacy/block/{b_id}", headers=_auth_headers(a_id))


# ---- restrict ----

def test_restrict_hides_comment_from_other_viewers_only(users):
    a_id, b_id = users
    db = TestSessionLocal()
    stranger = models.User(username="privacy_stranger", full_name="S", is_active=True)
    db.add(stranger)
    db.commit()
    db.refresh(stranger)
    stranger_id = stranger.id
    db.close()

    post = client.post(
        "/api/posts", headers=_auth_headers(a_id),
        data={"caption": "restrict test post"}, files={"file": _fake_image()},
    ).json()

    comment = client.post(
        f"/api/posts/{post['id']}/comments", headers=_auth_headers(b_id), json={"content": "restricted comment"}
    ).json()

    restrict_resp = client.post(f"/api/privacy/restrict/{b_id}", headers=_auth_headers(a_id))
    assert restrict_resp.status_code == 200
    assert restrict_resp.json()["is_restricted"] is True

    # stranger shouldn't see it
    stranger_view = client.get(f"/api/posts/{post['id']}/comments", headers=_auth_headers(stranger_id)).json()
    assert all(c["id"] != comment["id"] for c in stranger_view["items"])

    # post owner still sees it
    owner_view = client.get(f"/api/posts/{post['id']}/comments", headers=_auth_headers(a_id)).json()
    assert any(c["id"] == comment["id"] for c in owner_view["items"])

    # the restricted commenter still sees their own comment
    self_view = client.get(f"/api/posts/{post['id']}/comments", headers=_auth_headers(b_id)).json()
    assert any(c["id"] == comment["id"] for c in self_view["items"])

    unrestrict = client.delete(f"/api/privacy/restrict/{b_id}", headers=_auth_headers(a_id))
    assert unrestrict.json()["is_restricted"] is False
    stranger_view_after = client.get(f"/api/posts/{post['id']}/comments", headers=_auth_headers(stranger_id)).json()
    assert any(c["id"] == comment["id"] for c in stranger_view_after["items"])


# ---- mute ----

def test_mute_and_unmute_user_defaults_both_true(users):
    a_id, b_id = users
    resp = client.post(f"/api/privacy/mute/{b_id}", headers=_auth_headers(a_id), json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mute_posts"] is True
    assert body["mute_stories"] is True

    muted_list = client.get("/api/privacy/muted", headers=_auth_headers(a_id)).json()
    assert any(m["user"]["id"] == b_id for m in muted_list["items"])

    resp = client.post(
        f"/api/privacy/mute/{b_id}", headers=_auth_headers(a_id),
        json={"mute_posts": False, "mute_stories": True},
    )
    assert resp.json()["mute_posts"] is False

    unmute = client.delete(f"/api/privacy/mute/{b_id}", headers=_auth_headers(a_id))
    assert unmute.status_code == 200
    muted_list_after = client.get("/api/privacy/muted", headers=_auth_headers(a_id)).json()
    assert all(m["user"]["id"] != b_id for m in muted_list_after["items"])


def test_muted_users_posts_excluded_from_home_feed(users):
    a_id, b_id = users
    # a must follow b for b's posts to ever appear in a's home feed
    db = TestSessionLocal()
    db.add(models.Follow(follower_id=a_id, following_id=b_id))
    db.commit()
    db.close()

    post = client.post(
        "/api/posts", headers=_auth_headers(b_id),
        data={"caption": "should be muted"}, files={"file": _fake_image()},
    ).json()

    feed_before = client.get("/api/posts/feed", headers=_auth_headers(a_id)).json()
    assert any(p["id"] == post["id"] for p in feed_before["items"])

    client.post(f"/api/privacy/mute/{b_id}", headers=_auth_headers(a_id), json={"mute_posts": True, "mute_stories": False})

    feed_after = client.get("/api/posts/feed", headers=_auth_headers(a_id)).json()
    assert all(p["id"] != post["id"] for p in feed_after["items"])

    client.delete(f"/api/privacy/mute/{b_id}", headers=_auth_headers(a_id))
