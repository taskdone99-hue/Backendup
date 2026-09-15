"""
Tests for reel location support (previously posts/stories only):

  - POST /api/reels accepts location_id / location_name+coordinates,
    same as POST /api/posts
  - GET  /api/reels/{id} (via other endpoints returning ReelDetailOut)
    includes the resolved location
  - PUT  /api/videos/{id}/metadata can set/clear a reel's location
  - GET  /api/locations/{id}/reels lists reels tagged at a location

Same harness pattern as test/test_hashtags_and_media.py — private app +
private in-memory SQLite, mounting the real, unmodified routers.
"""

import os
import sys
from pathlib import Path

os.environ["SECRET_KEY"] = "test-secret-key-for-reel-location-tests"
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
from app.routers.content_routes import reels_router
from app.routers.video_routes import router as videos_router
from app.routers.location_routes import router as locations_router

Base.metadata.create_all(bind=test_engine)

test_app = FastAPI()
test_app.include_router(reels_router)
test_app.include_router(videos_router)
test_app.include_router(locations_router)


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


def _fake_video(name: str = "clip.mp4") -> tuple:
    return (name, b"fake video bytes not real mp4", "video/mp4")


@pytest.fixture(scope="module")
def seeded_user():
    db = TestSessionLocal()
    user = models.User(username="reeler", full_name="Reel Er", is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    user_id = user.id
    db.close()
    return user_id


def test_create_reel_with_flat_location_fields(seeded_user):
    resp = client.post(
        "/api/reels",
        headers=_auth_headers(seeded_user),
        data={
            "caption": "at the beach",
            "location_name": "Marina Beach",
            "location_latitude": 13.0500,
            "location_longitude": 80.2824,
        },
        files={"file": _fake_video()},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["location"]["name"] == "Marina Beach"
    assert body["location"]["latitude"] == 13.0500
    assert body["location_name"] == "Marina Beach"


def test_create_reel_without_location_leaves_it_null(seeded_user):
    resp = client.post(
        "/api/reels",
        headers=_auth_headers(seeded_user),
        data={"caption": "no place tagged"},
        files={"file": _fake_video()},
    )
    assert resp.status_code == 201
    assert resp.json()["location"] is None


def test_create_reel_invalid_latitude_rejected(seeded_user):
    resp = client.post(
        "/api/reels",
        headers=_auth_headers(seeded_user),
        data={"location_name": "Nowhere", "location_latitude": 999},
        files={"file": _fake_video()},
    )
    assert resp.status_code == 400


def test_create_reel_with_existing_location_id_reuses_it(seeded_user):
    first = client.post(
        "/api/reels",
        headers=_auth_headers(seeded_user),
        data={"location_name": "Charminar", "location_latitude": 17.3616, "location_longitude": 78.4747},
        files={"file": _fake_video()},
    ).json()
    loc_id = first["location"]["id"]

    second = client.post(
        "/api/reels",
        headers=_auth_headers(seeded_user),
        data={"location_id": loc_id},
        files={"file": _fake_video()},
    )
    assert second.status_code == 201
    assert second.json()["location"]["id"] == loc_id


def test_update_video_metadata_sets_and_clears_location(seeded_user):
    created = client.post(
        "/api/reels",
        headers=_auth_headers(seeded_user),
        data={"caption": "will set location later"},
        files={"file": _fake_video()},
    ).json()
    assert created["location"] is None

    set_resp = client.put(
        f"/api/videos/{created['id']}/metadata",
        headers=_auth_headers(seeded_user),
        json={"location": {"name": "Golconda Fort", "latitude": 17.3833, "longitude": 78.4011}},
    )
    assert set_resp.status_code == 200
    assert set_resp.json()["location"]["name"] == "Golconda Fort"

    clear_resp = client.put(
        f"/api/videos/{created['id']}/metadata",
        headers=_auth_headers(seeded_user),
        json={"location": None},
    )
    assert clear_resp.status_code == 200
    assert clear_resp.json()["location"] is None


def test_location_reels_endpoint_lists_tagged_reels(seeded_user):
    created = client.post(
        "/api/reels",
        headers=_auth_headers(seeded_user),
        data={"location_name": "Hussain Sagar", "location_latitude": 17.4239, "location_longitude": 78.4738},
        files={"file": _fake_video()},
    ).json()
    loc_id = created["location"]["id"]

    resp = client.get(f"/api/locations/{loc_id}/reels")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert any(item["id"] == created["id"] for item in body["items"])


def test_location_reels_endpoint_404_for_unknown_location():
    resp = client.get("/api/locations/999999/reels")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Optional multipart location fields: Swagger "Try it out" (and plain HTML
# forms) send an unfilled optional field as an empty string "" rather than
# omitting it. These fields must accept that the same as if they'd been
# left out entirely, instead of a 422 "unable to parse string as a number"
# error. See app/form_fields.py.
# --------------------------------------------------------------------------

def test_create_reel_without_location(seeded_user):
    """1. Reel without location: no location_* fields sent at all."""
    resp = client.post(
        "/api/reels",
        headers=_auth_headers(seeded_user),
        data={"caption": "just a clip, no place"},
        files={"file": _fake_video()},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["location"] is None
    assert body["location_name"] is None


def test_create_reel_with_location_id(seeded_user):
    """2. Reel with location_id: attaches an existing saved location."""
    seed = client.post(
        "/api/reels",
        headers=_auth_headers(seeded_user),
        data={
            "location_name": "Golkonda Fort",
            "location_latitude": 17.3833,
            "location_longitude": 78.4011,
        },
        files={"file": _fake_video()},
    ).json()
    loc_id = seed["location"]["id"]

    resp = client.post(
        "/api/reels",
        headers=_auth_headers(seeded_user),
        data={"caption": "back again", "location_id": loc_id},
        files={"file": _fake_video()},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["location"]["id"] == loc_id
    assert body["location"]["name"] == "Golkonda Fort"


def test_create_reel_with_new_location_details(seeded_user):
    """3. Reel with new location details: resolve/create via
    location_name + coordinates + the other location_* fields."""
    resp = client.post(
        "/api/reels",
        headers=_auth_headers(seeded_user),
        data={
            "caption": "new spot",
            "location_name": "Ramoji Film City",
            "location_address": "Anaspur Village",
            "location_city": "Hyderabad",
            "location_state": "Telangana",
            "location_country": "India",
            "location_latitude": 17.2543,
            "location_longitude": 78.6808,
        },
        files={"file": _fake_video()},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["location"]["name"] == "Ramoji Film City"
    assert body["location"]["city"] == "Hyderabad"
    assert body["location"]["latitude"] == 17.2543
    assert body["location"]["longitude"] == 78.6808


def test_create_reel_empty_optional_location_fields_not_rejected(seeded_user):
    """4. Empty-string optional location fields (what Swagger's "Try it
    out" sends for blank fields) must not 400/422 — they're treated the
    same as those fields being omitted entirely, so no location ends up
    attached."""
    resp = client.post(
        "/api/reels",
        headers=_auth_headers(seeded_user),
        data={
            "caption": "swagger blank fields",
            "location_id": "",
            "location_name": "",
            "location_latitude": "",
            "location_longitude": "",
            "location_address": "",
            "location_city": "",
            "location_state": "",
            "location_country": "",
            "location_place_id": "",
        },
        files={"file": _fake_video()},
    )
    assert resp.status_code == 201
    assert resp.json()["location"] is None


def test_create_reel_blank_coordinates_with_location_name_still_creates_location(seeded_user):
    """Blank lat/long shouldn't block resolving a location from name alone
    (coordinates stay unset on it, same as if the fields were omitted)."""
    resp = client.post(
        "/api/reels",
        headers=_auth_headers(seeded_user),
        data={
            "location_name": "Some Unnamed Spot",
            "location_latitude": "",
            "location_longitude": "",
            "location_id": "",
        },
        files={"file": _fake_video()},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["location"]["name"] == "Some Unnamed Spot"
    assert body["location"]["latitude"] is None
    assert body["location"]["longitude"] is None


def test_create_reel_invalid_latitude_still_rejected_with_real_value(seeded_user):
    """The empty-string fix must not loosen validation for an actual
    out-of-range value — only blank input is treated as absent."""
    resp = client.post(
        "/api/reels",
        headers=_auth_headers(seeded_user),
        data={"location_name": "Nowhere", "location_latitude": "999"},
        files={"file": _fake_video()},
    )
    assert resp.status_code == 400