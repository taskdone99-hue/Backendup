"""
Tests for:
  - GET /api/locations/nearby — Haversine-based nearby search
  - Per-photo captions on multi-media posts (media_captions on
    POST /api/posts and PUT /api/posts/:id/media)

Same harness pattern as the other new test files.
"""

import os
import sys
from pathlib import Path

os.environ["SECRET_KEY"] = "test-secret-key-for-nearby-captions-tests"
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
from app.routers.location_routes import router as locations_router
from app.routers.content_routes import router as posts_router

Base.metadata.create_all(bind=test_engine)

test_app = FastAPI()
test_app.include_router(locations_router)
test_app.include_router(posts_router)


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


def _make_user(username: str) -> int:
    db = TestSessionLocal()
    u = models.User(username=username, full_name=username, is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    uid = u.id
    db.close()
    return uid


# ---- Nearby Locations ----

def test_nearby_returns_locations_within_radius_sorted_by_distance():
    # Hyderabad-area coordinates: Charminar, Golconda Fort (~11km away),
    # and a far-away point (Mumbai, ~620km) that should never show up.
    charminar = client.post(
        "/api/locations",
        headers=_auth_headers(_make_user("nearby_u1")),
        json={"name": "Charminar", "latitude": 17.3616, "longitude": 78.4747},
    ).json()
    golconda = client.post(
        "/api/locations",
        headers=_auth_headers(_make_user("nearby_u2")),
        json={"name": "Golconda Fort", "latitude": 17.3833, "longitude": 78.4011},
    ).json()
    client.post(
        "/api/locations",
        headers=_auth_headers(_make_user("nearby_u3")),
        json={"name": "Gateway of India", "latitude": 18.9220, "longitude": 72.8347},
    )

    resp = client.get("/api/locations/nearby", params={"latitude": 17.3616, "longitude": 78.4747, "radius_km": 15})
    assert resp.status_code == 200
    body = resp.json()
    names = [item["name"] for item in body["items"]]
    assert "Charminar" in names
    assert "Golconda Fort" in names
    assert "Gateway of India" not in names

    # nearest first: Charminar (0 km, it's the exact search point) before Golconda
    assert names.index("Charminar") < names.index("Golconda Fort")
    charminar_row = next(i for i in body["items"] if i["name"] == "Charminar")
    assert charminar_row["distance_km"] < 0.01


def test_nearby_empty_when_nothing_in_range():
    resp = client.get("/api/locations/nearby", params={"latitude": 0.0, "longitude": 0.0, "radius_km": 1})
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_nearby_validates_latitude_range():
    resp = client.get("/api/locations/nearby", params={"latitude": 999, "longitude": 0})
    assert resp.status_code == 400


def test_nearby_respects_limit():
    uid = _make_user("nearby_limit_u")
    for i in range(5):
        client.post(
            "/api/locations",
            headers=_auth_headers(uid),
            json={"name": f"Spot {i}", "latitude": 12.9716 + i * 0.001, "longitude": 77.5946},
        )
    resp = client.get(
        "/api/locations/nearby",
        params={"latitude": 12.9716, "longitude": 77.5946, "radius_km": 10, "limit": 2},
    )
    assert resp.status_code == 200
    assert len(resp.json()["items"]) <= 2


# ---- Per-photo captions ----

def test_create_post_with_per_photo_captions():
    uid = _make_user("caption_u1")
    resp = client.post(
        "/api/posts",
        headers=_auth_headers(uid),
        data={
            "caption": "trip highlights",
            "media_captions": ["first one", "second one", "third one"],
        },
        files=[
            ("files", _fake_image("a.jpg")),
            ("files", _fake_image("b.jpg")),
            ("files", _fake_image("c.jpg")),
        ],
    )
    assert resp.status_code == 201
    body = resp.json()
    captions = [m["caption"] for m in body["media"]]
    assert captions == ["first one", "second one", "third one"]


def test_create_post_with_fewer_captions_than_files():
    uid = _make_user("caption_u2")
    resp = client.post(
        "/api/posts",
        headers=_auth_headers(uid),
        data={"media_captions": ["only the first"]},
        files=[("files", _fake_image("a.jpg")), ("files", _fake_image("b.jpg"))],
    )
    assert resp.status_code == 201
    captions = [m["caption"] for m in resp.json()["media"]]
    assert captions == ["only the first", None]


def test_create_post_too_many_captions_rejected():
    uid = _make_user("caption_u3")
    resp = client.post(
        "/api/posts",
        headers=_auth_headers(uid),
        data={"media_captions": ["one", "two", "three"]},
        files={"file": _fake_image()},
    )
    assert resp.status_code == 400


def test_create_post_without_captions_defaults_to_none():
    uid = _make_user("caption_u4")
    resp = client.post(
        "/api/posts", headers=_auth_headers(uid),
        data={"caption": "no per-photo captions here"}, files={"file": _fake_image()},
    )
    assert resp.status_code == 201
    assert resp.json()["media"][0]["caption"] is None


def test_update_post_media_replaces_captions():
    uid = _make_user("caption_u5")
    created = client.post(
        "/api/posts", headers=_auth_headers(uid),
        data={"media_captions": ["old caption"]}, files={"file": _fake_image()},
    ).json()

    updated = client.put(
        f"/api/posts/{created['id']}/media",
        headers=_auth_headers(uid),
        data={"media_captions": ["new caption one", "new caption two"]},
        files=[("files", _fake_image("x.jpg")), ("files", _fake_image("y.jpg"))],
    )
    assert updated.status_code == 200
    captions = [m["caption"] for m in updated.json()["media"]]
    assert captions == ["new caption one", "new caption two"]
