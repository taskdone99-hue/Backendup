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
    assert set(body.keys()) == {"users", "songs", "locations"}
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
