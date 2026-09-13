"""
Tests for the new creator-monetization surface:
  - GET  /api/monetization/earnings
  - GET  /api/monetization/earnings/summary
  - POST/GET/accept/reject/cancel /api/creator-collaborations
  - POST/GET/accept/reject/cancel/complete /api/brand-collaborations

Test harness notes: this mounts the *real*, unmodified routers
(monetization_routes, creator_collaboration_routes,
brand_collaboration_routes) onto a minimal standalone FastAPI app backed by
an isolated in-memory SQLite database, rather than importing the shared
app.main.app singleton. This is deliberate, not a shortcut: app.main.app is
a module-level singleton, so if this file's test run shared it with another
test file that also swaps app.database.engine/SessionLocal (e.g. a
test_search.py doing the same for its own isolated DB), whichever file's
import runs last would silently repoint every earlier file's
get_db override and engine at its own database mid-session — the app
object doesn't know it's being fought over. Mounting the real router
objects against a private app + private engine avoids that entirely while
still exercising the actual endpoint/service/schema code end-to-end over
HTTP, exactly as app.main would wire it.

One consequence: app.main's custom exception handlers (which normalize
HTTPException bodies to {"message": ...}, see test_smoke.py) aren't present
on this minimal app, so error bodies here come back in FastAPI's default
shape, {"detail": ...}. Tests assert on status codes (and "detail" where
checked) accordingly — this differs only in the response envelope, not in
status codes or behavior.

Run with:
    pip install -r requirements.txt pytest
    pytest test/test_creator_monetization.py -v
"""

import os
import sys
from pathlib import Path

os.environ["SECRET_KEY"] = "test-secret-key-for-creator-monetization-tests"
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

# ---- isolated in-memory SQLite for this test module only ----
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
from app.routers.monetization_routes import router as monetization_router
from app.routers.creator_collaboration_routes import router as creator_collab_router
from app.routers.brand_collaboration_routes import router as brand_collab_router

Base.metadata.create_all(bind=test_engine)

test_app = FastAPI()
test_app.include_router(monetization_router)
test_app.include_router(creator_collab_router)
test_app.include_router(brand_collab_router)


# Same two exception handlers as app.main, copied here (rather than
# importing app.main, which would pull in the shared singleton app +
# every other router) so validation/HTTPException bodies and status codes
# match production exactly: {"message": "..."} instead of FastAPI's default
# {"detail": [...]}, and pydantic ValidationError -> 400 instead of 422.
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
def seeded_users():
    """Two creators + one 'brand rep' user, seeded once for the module."""
    db = TestSessionLocal()
    creator_a = models.User(username="creator_a", full_name="Creator A", is_active=True)
    creator_b = models.User(username="creator_b", full_name="Creator B", is_active=True)
    brand_rep = models.User(
        username="brandrep",
        full_name="Brand Rep",
        is_active=True,
        account_type=models.AccountType.business,
        business_name="Acme Co",
    )
    db.add_all([creator_a, creator_b, brand_rep])
    db.commit()
    db.refresh(creator_a)
    db.refresh(creator_b)
    db.refresh(brand_rep)
    ids = {"creator_a": creator_a.id, "creator_b": creator_b.id, "brand_rep": brand_rep.id}
    db.close()
    return ids


# ---------------------------------------------------------------------
# Creator earnings ledger
# ---------------------------------------------------------------------

def test_earnings_empty_by_default(seeded_users):
    resp = client.get("/api/monetization/earnings", headers=_auth_headers(seeded_users["creator_a"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["total_earnings_cents"] == 0
    assert body["items"] == []


def test_earnings_summary_shape(seeded_users):
    resp = client.get("/api/monetization/earnings/summary", headers=_auth_headers(seeded_users["creator_a"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_earnings_cents"] == 0
    assert "monetization" in body
    assert body["monetization"]["monetization_enabled"] is False


def test_earnings_requires_auth():
    resp = client.get("/api/monetization/earnings")
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------
# Creator-to-creator collaboration requests
# ---------------------------------------------------------------------

def test_create_and_accept_creator_collaboration_credits_earnings(seeded_users):
    a, b = seeded_users["creator_a"], seeded_users["creator_b"]

    resp = client.post(
        "/api/creator-collaborations",
        json={
            "partner_user_id": b,
            "message": "Want to collab?",
            "proposed_amount_cents": 5000,
        },
        headers=_auth_headers(a),
    )
    assert resp.status_code == 201, resp.text
    request = resp.json()
    assert request["status"] == "pending"
    assert request["requester"]["id"] == a
    assert request["partner"]["id"] == b
    request_id = request["id"]

    # Only the invited partner (b) can accept.
    forbidden = client.post(f"/api/creator-collaborations/{request_id}/accept", headers=_auth_headers(a))
    assert forbidden.status_code == 403

    accepted = client.post(f"/api/creator-collaborations/{request_id}/accept", headers=_auth_headers(b))
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"

    # Accepting a second time is a conflict.
    again = client.post(f"/api/creator-collaborations/{request_id}/accept", headers=_auth_headers(b))
    assert again.status_code == 409

    # The proposed flat payout was credited to the partner (b)'s ledger.
    earnings = client.get("/api/monetization/earnings", headers=_auth_headers(b))
    assert earnings.status_code == 200
    body = earnings.json()
    assert body["total_earnings_cents"] == 5000
    assert body["items"][0]["source_type"] == "creator_collaboration"


def test_reject_creator_collaboration(seeded_users):
    a, b = seeded_users["creator_a"], seeded_users["creator_b"]
    resp = client.post(
        "/api/creator-collaborations",
        json={"partner_user_id": a},
        headers=_auth_headers(b),
    )
    assert resp.status_code == 201
    request_id = resp.json()["id"]

    rejected = client.post(f"/api/creator-collaborations/{request_id}/reject", headers=_auth_headers(a))
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"


def test_cannot_request_collaboration_with_self(seeded_users):
    a = seeded_users["creator_a"]
    resp = client.post(
        "/api/creator-collaborations",
        json={"partner_user_id": a},
        headers=_auth_headers(a),
    )
    assert resp.status_code == 400


def test_list_incoming_and_outgoing_requests(seeded_users):
    a, b = seeded_users["creator_a"], seeded_users["creator_b"]
    client.post("/api/creator-collaborations", json={"partner_user_id": b}, headers=_auth_headers(a))

    incoming = client.get(
        "/api/creator-collaborations", params={"direction": "incoming"}, headers=_auth_headers(b)
    )
    assert incoming.status_code == 200
    assert incoming.json()["total"] >= 1
    for item in incoming.json()["items"]:
        assert item["partner"]["id"] == b


# ---------------------------------------------------------------------
# Brand collaborations
# ---------------------------------------------------------------------

def test_create_and_accept_brand_collaboration_credits_earnings(seeded_users):
    brand_rep, creator = seeded_users["brand_rep"], seeded_users["creator_a"]

    resp = client.post(
        "/api/brand-collaborations",
        json={
            "creator_user_id": creator,
            "brand_name": "Acme Co",
            "brand_contact_email": "deals@acme.example",
            "campaign_title": "Summer launch",
            "campaign_description": "Post 3 reels featuring our product",
            "offer_amount_cents": 250000,
            "currency": "INR",
            "deliverables": "3 reels + 1 story",
        },
        headers=_auth_headers(brand_rep),
    )
    assert resp.status_code == 201, resp.text
    offer = resp.json()
    assert offer["status"] == "pending"
    offer_id = offer["id"]

    # Only the targeted creator can accept.
    forbidden = client.post(f"/api/brand-collaborations/{offer_id}/accept", headers=_auth_headers(brand_rep))
    assert forbidden.status_code == 403

    accepted = client.post(f"/api/brand-collaborations/{offer_id}/accept", headers=_auth_headers(creator))
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"

    summary = client.get("/api/monetization/earnings/summary", headers=_auth_headers(creator))
    assert summary.status_code == 200
    assert summary.json()["by_source"]["brand_collaboration"] == 250000

    completed = client.post(f"/api/brand-collaborations/{offer_id}/complete", headers=_auth_headers(creator))
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"


def test_brand_collaboration_invalid_email_rejected(seeded_users):
    brand_rep, creator = seeded_users["brand_rep"], seeded_users["creator_b"]
    resp = client.post(
        "/api/brand-collaborations",
        json={
            "creator_user_id": creator,
            "brand_name": "Acme Co",
            "brand_contact_email": "not-an-email",
            "campaign_title": "Bad email test",
            "offer_amount_cents": 1000,
        },
        headers=_auth_headers(brand_rep),
    )
    assert resp.status_code == 400


def test_brand_collaboration_cancel_by_creator_forbidden(seeded_users):
    brand_rep, creator = seeded_users["brand_rep"], seeded_users["creator_b"]
    resp = client.post(
        "/api/brand-collaborations",
        json={
            "creator_user_id": creator,
            "brand_name": "Acme Co",
            "campaign_title": "Cancel test",
            "offer_amount_cents": 1000,
        },
        headers=_auth_headers(brand_rep),
    )
    assert resp.status_code == 201, resp.text
    offer_id = resp.json()["id"]

    forbidden = client.post(f"/api/brand-collaborations/{offer_id}/cancel", headers=_auth_headers(creator))
    assert forbidden.status_code == 403

    cancelled = client.post(f"/api/brand-collaborations/{offer_id}/cancel", headers=_auth_headers(brand_rep))
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
