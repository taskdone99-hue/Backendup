"""
Tests for:
  - multi-file post creation (POST /api/posts, PUT /api/posts/:id/media)
  - hashtag persistence + discovery (GET /api/hashtags/*)

Test harness notes: same pattern as test/test_search.py — mounts the real,
unmodified content_routes/hashtag_routes routers onto a private FastAPI app
+ private in-memory SQLite database, rather than the shared app.main.app
singleton (see test_creator_monetization.py's harness note for why).

Error bodies here come back in FastAPI's default {"detail": ...} shape
since app.main's custom exception handlers aren't mounted on this minimal
app, except where copied below to match production's {"message": ...} shape
for the 400s this suite actually asserts on.

Run with:
    pip install -r requirements.txt pytest
    pytest test/test_hashtags_and_media.py -v
"""

import os
import sys
from pathlib import Path

os.environ["SECRET_KEY"] = "test-secret-key-for-hashtag-media-tests"
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
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
database.engine = test_engine
database.SessionLocal = TestSessionLocal

from app import models
from app.database import Base, get_db
from app.auth import create_access_token
from app.routers.content_routes import router as posts_router
from app.routers.hashtag_routes import router as hashtags_router

Base.metadata.create_all(bind=test_engine)

test_app = FastAPI()
test_app.include_router(posts_router)
test_app.include_router(hashtags_router)


@test_app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    def clean_msg(msg: str) -> str:
        prefix = "Value error, "
        return msg[len(prefix):] if msg.startswith(prefix) else msg

    first = exc.errors()[0]
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"message": clean_msg(first["msg"])},
    )


@test_app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"message": exc.detail},
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
    """(filename, content, content_type) tuple for requests/httpx `files=`.
    save_upload_file only checks the declared content_type, not real image
    bytes, so any non-empty payload works here."""
    return (name, b"\xff\xd8\xff\xe0fakejpegbytes", "image/jpeg")


@pytest.fixture(scope="module")
def seeded_user():
    db = TestSessionLocal()
    user = models.User(username="poster", full_name="Poster One", is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    user_id = user.id
    db.close()
    return user_id


# ---- multi-file post creation ----

def test_create_post_single_file_is_backward_compatible(seeded_user):
    resp = client.post(
        "/api/posts",
        headers=_auth_headers(seeded_user),
        data={"caption": "just one photo"},
        files={"file": _fake_image()},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["media_count"] == 1
    assert len(body["media"]) == 1
    assert body["media"][0]["media_url"] == body["media_url"]


def test_create_post_with_multiple_files_returns_all_media(seeded_user):
    resp = client.post(
        "/api/posts",
        headers=_auth_headers(seeded_user),
        data={"caption": "carousel #hyderabad #food"},
        files=[
            ("files", _fake_image("one.jpg")),
            ("files", _fake_image("two.jpg")),
            ("files", _fake_image("three.jpg")),
        ],
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["media_count"] == 3
    assert len(body["media"]) == 3
    urls = {m["media_url"] for m in body["media"]}
    assert len(urls) == 3  # each file got its own saved url
    positions = [m["position"] for m in body["media"]]
    assert positions == [0, 1, 2]
    # first uploaded file mirrors the flat legacy fields
    assert body["media_url"] == body["media"][0]["media_url"]


def test_create_post_requires_at_least_one_file(seeded_user):
    resp = client.post(
        "/api/posts",
        headers=_auth_headers(seeded_user),
        data={"caption": "no media attached"},
    )
    assert resp.status_code == 400


def test_update_post_media_replaces_whole_carousel(seeded_user):
    created = client.post(
        "/api/posts",
        headers=_auth_headers(seeded_user),
        data={"caption": "before replace"},
        files=[("files", _fake_image("a.jpg")), ("files", _fake_image("b.jpg"))],
    ).json()
    assert created["media_count"] == 2

    replaced = client.put(
        f"/api/posts/{created['id']}/media",
        headers=_auth_headers(seeded_user),
        files=[("files", _fake_image("c.jpg"))],
    )
    assert replaced.status_code == 200
    body = replaced.json()
    assert body["media_count"] == 1
    assert body["media"][0]["media_url"] != created["media"][0]["media_url"]


# ---- hashtags ----

def test_create_post_persists_hashtags(seeded_user):
    resp = client.post(
        "/api/posts",
        headers=_auth_headers(seeded_user),
        data={"caption": "Loving #Hyderabad and #hyderabad food #Biryani"},
        files={"file": _fake_image()},
    )
    assert resp.status_code == 201
    # case-insensitive de-dupe: #Hyderabad and #hyderabad are the same tag
    assert sorted(resp.json()["hashtags"]) == ["biryani", "hyderabad"]


def test_get_hashtag_returns_posts_count(seeded_user):
    client.post(
        "/api/posts",
        headers=_auth_headers(seeded_user),
        data={"caption": "another #biryani post"},
        files={"file": _fake_image()},
    )
    resp = client.get("/api/hashtags/biryani")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "biryani"
    assert body["posts_count"] >= 2


def test_get_hashtag_is_case_and_hash_insensitive(seeded_user):
    resp = client.get("/api/hashtags/%23BIRYANI")  # "#BIRYANI" url-encoded
    assert resp.status_code == 200
    assert resp.json()["name"] == "biryani"


def test_get_hashtag_not_found(seeded_user):
    resp = client.get("/api/hashtags/nosuchtagexists")
    assert resp.status_code == 404


def test_get_hashtag_posts_lists_matching_posts_only(seeded_user):
    resp = client.get("/api/hashtags/biryani/posts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 2
    for item in body["items"]:
        assert "biryani" in item["hashtags"]


def test_update_post_caption_resyncs_hashtags(seeded_user):
    created = client.post(
        "/api/posts",
        headers=_auth_headers(seeded_user),
        data={"caption": "#oldtag here"},
        files={"file": _fake_image()},
    ).json()
    assert created["hashtags"] == ["oldtag"]

    updated = client.put(
        f"/api/posts/{created['id']}",
        headers=_auth_headers(seeded_user),
        json={"caption": "#newtag replaces it"},
    )
    assert updated.status_code == 200
    assert updated.json()["hashtags"] == ["newtag"]

    # the old tag's post count should no longer include this post
    old_tag_posts = client.get("/api/hashtags/oldtag/posts").json()
    assert all(item["id"] != created["id"] for item in old_tag_posts["items"])

    new_tag_posts = client.get("/api/hashtags/newtag/posts").json()
    assert any(item["id"] == created["id"] for item in new_tag_posts["items"])


def test_trending_hashtags_orders_by_recent_post_count(seeded_user):
    # #biryani already has 2+ posts from earlier tests in this module;
    # give a fresh tag exactly one post so it should rank behind biryani.
    client.post(
        "/api/posts",
        headers=_auth_headers(seeded_user),
        data={"caption": "#onepost only"},
        files={"file": _fake_image()},
    )
    resp = client.get("/api/hashtags/trending")
    assert resp.status_code == 200
    body = resp.json()
    names = [item["name"] for item in body["items"]]
    assert "biryani" in names
    assert "onepost" in names
    assert names.index("biryani") < names.index("onepost")
