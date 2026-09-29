"""
Tests for GET /api/search — see app/routers/search_routes.py.

Test harness notes: mounts the real, unmodified search_routes.router onto a
private FastAPI app + private in-memory SQLite database, rather than the
shared app.main.app singleton — see the harness note at the top of
test_creator_monetization.py for why (module-level app/engine singletons
get fought over when multiple test files each swap them for their own
isolated database in the same pytest session).

Error bodies here come back in FastAPI's default {"detail": ...} shape
since app.main's custom exception handlers aren't mounted on this minimal
app — tests assert on status codes accordingly.

Run with:
    pip install -r requirements.txt pytest
    pytest test/test_search.py -v
"""

import os
import sys
from pathlib import Path

os.environ["SECRET_KEY"] = "test-secret-key-for-search-tests"
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("DB_USER", "test")
os.environ.setdefault("DB_PASSWORD", "test")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

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

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import models
from app.database import Base, get_db
from app.auth import create_access_token
from app.routers.search_routes import router as search_router

Base.metadata.create_all(bind=test_engine)

test_app = FastAPI()
test_app.include_router(search_router)


# Mirrors app.main's two exception handlers exactly (copied rather than
# imported, to avoid pulling in the shared app.main.app singleton — see
# test_creator_monetization.py's harness note) so error bodies/status codes
# match production: {"message": ...} and 400 instead of FastAPI's default
# {"detail": ...} / 422 for validation errors.
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


@pytest.fixture(scope="module")
def seeded_data():
    db = TestSessionLocal()

    searcher = models.User(username="searcher", full_name="Search Er", is_active=True)
    anjali = models.User(username="anjali_r", full_name="Anjali Rao", is_active=True)
    rahul = models.User(username="rahul99", full_name="Rahul Gupta", is_active=True)
    db.add_all([searcher, anjali, rahul])
    db.commit()
    db.refresh(searcher)
    db.refresh(anjali)
    db.refresh(rahul)

    song1 = models.Audio(title="Kesariya", artist="Arijit Singh", audio_url="https://example.com/a1.mp3")
    song2 = models.Audio(title="Naatu Naatu", artist="Rahul Sipligunj", audio_url="https://example.com/a2.mp3")
    db.add_all([song1, song2])

    loc1 = models.Location(name="Charminar", city="Hyderabad", country="India")
    loc2 = models.Location(name="Gateway of India", city="Mumbai", country="India")
    db.add_all([loc1, loc2])

    # Hashtags. "anjali_wedding" vs "anjalixwedding" exist to prove '_' in a
    # query is matched literally rather than acting as a LIKE wildcard.
    tag_anjali = models.Hashtag(name="anjali")
    tag_wedding = models.Hashtag(name="anjali_wedding")
    tag_lookalike = models.Hashtag(name="anjalixwedding")
    tag_food = models.Hashtag(name="hyderabadfood")
    db.add_all([tag_anjali, tag_wedding, tag_lookalike, tag_food])
    db.commit()

    post1 = models.Post(user_id=anjali.id, media_url="/static/posts/1.jpg", caption="#anjali")
    post2 = models.Post(user_id=rahul.id, media_url="/static/posts/2.jpg", caption="#anjali #anjali_wedding")
    db.add_all([post1, post2])
    db.commit()
    db.add_all([
        models.PostHashtag(post_id=post1.id, hashtag_id=tag_anjali.id),
        models.PostHashtag(post_id=post2.id, hashtag_id=tag_anjali.id),
        models.PostHashtag(post_id=post2.id, hashtag_id=tag_wedding.id),
    ])
    db.commit()

    ids = {
        "searcher": searcher.id,
        "anjali": anjali.id,
        "rahul": rahul.id,
    }
    db.close()
    return ids


def test_search_requires_auth():
    resp = client.get("/api/search", params={"q": "anjali"})
    assert resp.status_code in (401, 403)


def test_search_users_by_username_substring(seeded_data):
    resp = client.get(
        "/api/search",
        params={"q": "anjali", "type": "users"},
        headers=_auth_headers(seeded_data["searcher"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["username"] == "anjali_r"


def test_search_users_by_id(seeded_data):
    rahul_id = seeded_data["rahul"]
    resp = client.get(
        "/api/search",
        params={"q": str(rahul_id), "type": "users"},
        headers=_auth_headers(seeded_data["searcher"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert any(item["id"] == rahul_id for item in body["items"])


def test_search_songs_by_title_and_artist(seeded_data):
    by_title = client.get(
        "/api/search",
        params={"q": "kesariya", "type": "songs"},
        headers=_auth_headers(seeded_data["searcher"]),
    )
    assert by_title.status_code == 200
    assert by_title.json()["total"] == 1
    assert by_title.json()["items"][0]["title"] == "Kesariya"

    by_artist = client.get(
        "/api/search",
        params={"q": "rahul sipligunj", "type": "songs"},
        headers=_auth_headers(seeded_data["searcher"]),
    )
    assert by_artist.status_code == 200
    assert by_artist.json()["total"] == 1
    assert by_artist.json()["items"][0]["artist"] == "Rahul Sipligunj"


def test_search_locations_by_name_and_city(seeded_data):
    by_name = client.get(
        "/api/search",
        params={"q": "charminar", "type": "locations"},
        headers=_auth_headers(seeded_data["searcher"]),
    )
    assert by_name.status_code == 200
    assert by_name.json()["total"] == 1

    by_city = client.get(
        "/api/search",
        params={"q": "mumbai", "type": "locations"},
        headers=_auth_headers(seeded_data["searcher"]),
    )
    assert by_city.status_code == 200
    assert by_city.json()["items"][0]["name"] == "Gateway of India"


def test_search_all_returns_grouped_results(seeded_data):
    resp = client.get(
        "/api/search",
        params={"q": "rahul", "type": "all"},
        headers=_auth_headers(seeded_data["searcher"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"users", "songs", "locations", "hashtags"}
    # "rahul" matches the user rahul99 and the song artist "Rahul Sipligunj"
    assert any(u["username"] == "rahul99" for u in body["users"])
    assert any(s["artist"] == "Rahul Sipligunj" for s in body["songs"])


def test_search_is_case_insensitive(seeded_data):
    resp = client.get(
        "/api/search",
        params={"q": "CHARMINAR", "type": "locations"},
        headers=_auth_headers(seeded_data["searcher"]),
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


def test_search_pagination(seeded_data):
    resp = client.get(
        "/api/search",
        params={"q": "a", "type": "users", "limit": 1, "offset": 0},
        headers=_auth_headers(seeded_data["searcher"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 1
    assert len(body["items"]) <= 1


# ---------------------------------------------------------------- hashtags

def _search(seeded_data, **params):
    return client.get(
        "/api/search", params=params, headers=_auth_headers(seeded_data["searcher"])
    )


def test_search_all_for_anjali_returns_every_group(seeded_data):
    """The exact request that was returning HTTP 500 in production."""
    resp = _search(seeded_data, q="anjali")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"users", "songs", "locations", "hashtags"}
    assert [u["username"] for u in body["users"]] == ["anjali_r"]
    assert body["songs"] == [] and body["locations"] == []
    assert {h["name"] for h in body["hashtags"]} == {"anjali", "anjali_wedding", "anjalixwedding"}


def test_search_all_keeps_users_songs_and_locations_alongside_hashtags(seeded_data):
    resp = _search(seeded_data, q="rahul")
    body = resp.json()
    assert any(u["username"] == "rahul99" for u in body["users"])
    assert any(s["artist"] == "Rahul Sipligunj" for s in body["songs"])
    assert body["hashtags"] == []

    resp = _search(seeded_data, q="hyderabad")
    body = resp.json()
    assert any(loc["name"] == "Charminar" for loc in body["locations"])
    assert [h["name"] for h in body["hashtags"]] == ["hyderabadfood"]


def test_search_hashtags_type_is_paginated_with_post_counts(seeded_data):
    resp = _search(seeded_data, q="anjali", type="hashtags")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3 and body["limit"] == 10 and body["offset"] == 0
    counts = {h["name"]: h["posts_count"] for h in body["items"]}
    assert counts == {"anjali": 2, "anjali_wedding": 1, "anjalixwedding": 0}
    # Same item shape as GET /api/hashtags/{name}: exactly name + posts_count.
    assert all(set(h.keys()) == {"name", "posts_count"} for h in body["items"])


def test_search_hashtags_ignores_leading_hash_and_case(seeded_data):
    plain = _search(seeded_data, q="anjali", type="hashtags").json()
    hashed = _search(seeded_data, q="#Anjali", type="hashtags").json()
    assert hashed == plain


def test_search_hashtags_pagination(seeded_data):
    first = _search(seeded_data, q="anjali", type="hashtags", limit=2, offset=0).json()
    second = _search(seeded_data, q="anjali", type="hashtags", limit=2, offset=2).json()
    assert first["total"] == second["total"] == 3
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    names = [h["name"] for h in first["items"] + second["items"]]
    assert names == sorted(names) and len(set(names)) == 3


def test_search_all_hashtags_group_is_capped_to_limit(seeded_data):
    body = _search(seeded_data, q="anjali", limit=1).json()
    assert len(body["hashtags"]) == 1


def test_search_hashtags_underscore_and_percent_are_literal(seeded_data):
    # '_' is a LIKE wildcard unless escaped: "anjali_" must NOT match "anjalixwedding".
    body = _search(seeded_data, q="anjali_", type="hashtags").json()
    assert [h["name"] for h in body["items"]] == ["anjali_wedding"]
    # '%' must not turn into match-everything.
    assert _search(seeded_data, q="%", type="hashtags").json()["total"] == 0


def test_search_hashtags_bare_hash_matches_nothing(seeded_data):
    body = _search(seeded_data, q="#", type="hashtags").json()
    assert body["total"] == 0 and body["items"] == []
    assert _search(seeded_data, q="#").json()["hashtags"] == []


def test_search_rejects_unknown_type(seeded_data):
    assert _search(seeded_data, q="anjali", type="bogus").status_code == 400
