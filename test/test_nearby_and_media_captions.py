"""
Tests for:
  - GET /api/locations/nearby — Haversine-based nearby search, paginated
    (total/limit/offset/items, same shape as GET /api/locations/search)
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


def _make_location(name: str, latitude: float, longitude: float) -> models.Location:
    """Seed a Location row directly — there's no POST /api/locations
    endpoint (locations are only ever created inline via find_or_create_location
    at post/story/reel creation), so tests that need saved locations go
    straight to the DB, same as test_search.py's seeded_data fixture."""
    db = TestSessionLocal()
    loc = models.Location(name=name, latitude=latitude, longitude=longitude)
    db.add(loc)
    db.commit()
    db.refresh(loc)
    db.close()
    return loc


# ---- Nearby Locations ----

def test_nearby_returns_locations_within_radius_sorted_by_distance():
    # Hyderabad-area coordinates: Charminar, Golconda Fort (~11km away),
    # and a far-away point (Mumbai, ~620km) that should never show up.
    _make_location("Charminar", 17.3616, 78.4747)
    _make_location("Golconda Fort", 17.3833, 78.4011)
    _make_location("Gateway of India", 18.9220, 72.8347)

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
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_nearby_validates_latitude_range():
    resp = client.get("/api/locations/nearby", params={"latitude": 999, "longitude": 0})
    assert resp.status_code == 400


def test_nearby_respects_limit():
    for i in range(5):
        _make_location(f"Spot {i}", 12.9716 + i * 0.001, 77.5946)
    resp = client.get(
        "/api/locations/nearby",
        params={"latitude": 12.9716, "longitude": 77.5946, "radius_km": 10, "limit": 2},
    )
    assert resp.status_code == 200
    assert len(resp.json()["items"]) <= 2


def test_nearby_pagination_default_offset_and_shape():
    for i in range(3):
        _make_location(f"Page Spot {i}", 40.0 + i * 0.001, 116.0)
    resp = client.get(
        "/api/locations/nearby",
        params={"latitude": 40.0, "longitude": 116.0, "radius_km": 10},
    )
    assert resp.status_code == 200
    body = resp.json()
    # same total/limit/offset/items shape as GET /search
    assert set(body.keys()) == {"total", "limit", "offset", "items"}
    assert body["offset"] == 0
    assert body["limit"] == 20
    assert body["total"] >= 3


def test_nearby_pagination_second_page_has_no_overlap_and_stays_sorted():
    latitude, longitude = 41.0, 117.0
    for i in range(5):
        _make_location(f"Paged {i}", latitude + i * 0.001, longitude)

    page1 = client.get(
        "/api/locations/nearby",
        params={"latitude": latitude, "longitude": longitude, "radius_km": 10, "limit": 2, "offset": 0},
    ).json()
    page2 = client.get(
        "/api/locations/nearby",
        params={"latitude": latitude, "longitude": longitude, "radius_km": 10, "limit": 2, "offset": 2},
    ).json()

    assert page1["total"] == page2["total"] == 5
    names1 = [i["name"] for i in page1["items"]]
    names2 = [i["name"] for i in page2["items"]]
    assert len(names1) == 2
    assert len(names2) == 2
    assert set(names1).isdisjoint(names2)  # no overlap between pages

    # distances across the two pages stay non-decreasing (global sort preserved
    # across the offset boundary, not just within each page)
    all_distances = [i["distance_km"] for i in page1["items"]] + [i["distance_km"] for i in page2["items"]]
    assert all_distances == sorted(all_distances)


def test_nearby_pagination_offset_past_total_returns_empty_items():
    _make_location("Solo Spot", 5.0, 5.0)
    resp = client.get(
        "/api/locations/nearby",
        params={"latitude": 5.0, "longitude": 5.0, "radius_km": 10, "offset": 50},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] >= 1


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
