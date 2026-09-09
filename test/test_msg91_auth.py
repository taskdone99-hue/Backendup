"""
Tests for MSG91 OTP Widget access-token verification
(POST /api/auth/verify-msg91-token) in app/routers/auth_routes.py.

Notes on the test harness:

app/main.py in this checkout does not contain a FastAPI application (it
currently holds a duplicate of app/models.py — see the change report).
Since that's unrelated to this OTP work and outside its scope, these
tests build a minimal standalone FastAPI app that mounts the *real*,
unmodified `app.routers.auth_routes.router` (same router object the
real app would use) against an isolated SQLite database, exactly the
way app.main would. This exercises the actual endpoint/service/schema
code end-to-end over HTTP, without depending on the broken main.py.

One consequence: the real app apparently has a custom exception handler
(see test_smoke.py, which asserts error bodies of the shape
{"message": ...}) that must live in the missing part of main.py. This
minimal test app doesn't have that handler, so HTTPException bodies here
come back in FastAPI's default shape, {"detail": ...}, instead. Tests
assert on `detail` accordingly — once main.py is restored, the error
*status codes* and *text* asserted here still hold; only the envelope
key would differ, exactly as it does for every other endpoint's errors.

Run with:
    pip install -r requirements.txt pytest
    pytest tests/test_msg91_auth.py -v
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ---- environment must be set before importing any app module ----
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-msg91-tests")
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("DB_USER", "test")
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("MSG91_AUTHKEY", "test-msg91-authkey")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

import app.database as database

# ---- isolated in-memory SQLite for every test run ----
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
from app.routers.auth_routes import router as auth_router
import app.services.msg91_service as msg91_service

Base.metadata.create_all(bind=test_engine)

test_app = FastAPI()
test_app.include_router(auth_router)


def override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


test_app.dependency_overrides[get_db] = override_get_db

client = TestClient(test_app)

VERIFY_URL = "/api/auth/verify-msg91-token"


def _reset_db():
    db = TestSessionLocal()
    try:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(table.delete())
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _clean_db():
    _reset_db()
    yield
    _reset_db()


class FakeMSG91Response:
    """Stand-in for requests.Response, only what verify_msg91_access_token uses."""

    def __init__(self, json_body, status_code=200):
        self._json_body = json_body
        self.status_code = status_code

    def json(self):
        return self._json_body


def _mock_msg91_success(identifier="919876543210"):
    return patch(
        "app.services.msg91_service.requests.post",
        return_value=FakeMSG91Response({"type": "success", "message": identifier}),
    )


def _mock_msg91_error(message="Invalid access-token"):
    return patch(
        "app.services.msg91_service.requests.post",
        return_value=FakeMSG91Response({"type": "error", "message": message}),
    )


def _mock_msg91_network_failure():
    import requests

    return patch(
        "app.services.msg91_service.requests.post",
        side_effect=requests.exceptions.ConnectionError("connection refused"),
    )


def _mock_msg91_server_error():
    return patch(
        "app.services.msg91_service.requests.post",
        return_value=FakeMSG91Response({"type": "error", "message": "upstream error"}, status_code=503),
    )


# ---------------------------------------------------------------------------
# 1. Valid MSG91 access token -> 200, verified user, tokens issued
# ---------------------------------------------------------------------------

def test_valid_access_token_returns_token_pair():
    with _mock_msg91_success("919876543210"):
        r = client.post(VERIFY_URL, json={"access_token": "good-token"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["user"]["phone_number"] == "+919876543210"
    assert body["user"]["is_phone_verified"] is True


def test_valid_access_token_calls_msg91_with_authkey_and_never_leaks_it():
    with _mock_msg91_success("919876543210") as mock_post:
        r = client.post(VERIFY_URL, json={"access_token": "good-token"})

    assert r.status_code == 200
    # Backend must call MSG91 server-to-server with authkey from env ...
    mock_post.assert_called_once()
    call_args, call_kwargs = mock_post.call_args
    assert call_args[0] == "https://control.msg91.com/api/v5/widget/verifyAccessToken"
    assert call_kwargs["json"]["authkey"] == "test-msg91-authkey"
    assert call_kwargs["json"]["access-token"] == "good-token"
    # ... and the authkey must never appear anywhere in the response we send back.
    assert "test-msg91-authkey" not in r.text


# ---------------------------------------------------------------------------
# 2. Invalid access token -> 400, no tokens issued, no user created
# ---------------------------------------------------------------------------

def test_invalid_access_token_returns_400():
    with _mock_msg91_error("Invalid access-token"):
        r = client.post(VERIFY_URL, json={"access_token": "bad-token"})

    assert r.status_code == 400
    assert "Invalid or expired access token" in r.json()["detail"]

    db = TestSessionLocal()
    try:
        assert db.query(models.User).count() == 0
    finally:
        db.close()


def test_empty_access_token_rejected_before_calling_msg91():
    with _mock_msg91_success() as mock_post:
        r = client.post(VERIFY_URL, json={"access_token": ""})

    # Pydantic min_length=1 rejects this at the schema level -> 422, and
    # MSG91 is never even contacted.
    assert r.status_code == 422
    mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# 3. MSG91 API failure (network error / 5xx) -> 502, no partial user state
# ---------------------------------------------------------------------------

def test_msg91_network_failure_returns_502():
    with _mock_msg91_network_failure():
        r = client.post(VERIFY_URL, json={"access_token": "any-token"})

    assert r.status_code == 502
    assert "try again shortly" in r.json()["detail"]

    db = TestSessionLocal()
    try:
        assert db.query(models.User).count() == 0
    finally:
        db.close()


def test_msg91_server_error_returns_502():
    with _mock_msg91_server_error():
        r = client.post(VERIFY_URL, json={"access_token": "any-token"})

    assert r.status_code == 502


def test_msg91_not_configured_returns_500():
    original = msg91_service.MSG91_AUTHKEY
    msg91_service.MSG91_AUTHKEY = None
    try:
        with _mock_msg91_success() as mock_post:
            r = client.post(VERIFY_URL, json={"access_token": "any-token"})
        assert r.status_code == 500
        assert "not configured" in r.json()["detail"]
        mock_post.assert_not_called()
    finally:
        msg91_service.MSG91_AUTHKEY = original


# ---------------------------------------------------------------------------
# 4. User creation after successful verification
# ---------------------------------------------------------------------------

def test_creates_new_user_with_placeholder_username():
    db = TestSessionLocal()
    try:
        assert db.query(models.User).count() == 0
    finally:
        db.close()

    with _mock_msg91_success("919876543210"):
        r = client.post(VERIFY_URL, json={"access_token": "good-token"})

    assert r.status_code == 200
    db = TestSessionLocal()
    try:
        users = db.query(models.User).all()
        assert len(users) == 1
        assert users[0].phone_number == "+919876543210"
        assert users[0].is_phone_verified is True
        assert users[0].username  # placeholder username was generated
    finally:
        db.close()


def test_creates_user_from_pending_signup_if_registered_first():
    db = TestSessionLocal()
    try:
        pending = models.PendingSignup(
            identifier="+919876543210",
            channel=models.OTPChannel.phone,
            username="anjali_ml",
            hashed_password="irrelevant-hash",
            date_of_birth=__import__("datetime").date(2000, 1, 1),
        )
        db.add(pending)
        db.commit()
    finally:
        db.close()

    with _mock_msg91_success("919876543210"):
        r = client.post(VERIFY_URL, json={"access_token": "good-token"})

    assert r.status_code == 200
    body = r.json()
    assert body["user"]["username"] == "anjali_ml"

    db = TestSessionLocal()
    try:
        # Pending signup row is consumed, not left behind.
        assert db.query(models.PendingSignup).count() == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 5. Existing user login (second verification for the same number)
# ---------------------------------------------------------------------------

def test_existing_user_logs_in_without_creating_duplicate():
    with _mock_msg91_success("919876543210"):
        first = client.post(VERIFY_URL, json={"access_token": "token-1"})
    assert first.status_code == 200
    first_user_id = first.json()["user"]["id"]

    with _mock_msg91_success("919876543210"):
        second = client.post(VERIFY_URL, json={"access_token": "token-2"})
    assert second.status_code == 200
    second_user_id = second.json()["user"]["id"]

    assert first_user_id == second_user_id

    db = TestSessionLocal()
    try:
        assert db.query(models.User).filter(
            models.User.phone_number == "+919876543210"
        ).count() == 1
    finally:
        db.close()

    # Refresh tokens differ per login (each call issues a fresh one).
    assert first.json()["refresh_token"] != second.json()["refresh_token"]


# ---------------------------------------------------------------------------
# 6. JWT generation
# ---------------------------------------------------------------------------

def test_issued_access_token_is_a_valid_jwt_for_the_verified_user():
    with _mock_msg91_success("919876543210"):
        r = client.post(VERIFY_URL, json={"access_token": "good-token"})

    assert r.status_code == 200
    body = r.json()

    payload = jwt.decode(
        body["access_token"],
        os.environ["SECRET_KEY"],
        algorithms=["HS256"],
    )
    assert payload["type"] == "access"
    assert int(payload["sub"]) == body["user"]["id"]

    # And that access token actually works against a protected endpoint.
    me = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["id"] == body["user"]["id"]


# ---------------------------------------------------------------------------
# 7. Email contact point (not just mobile) is handled too
# ---------------------------------------------------------------------------

def test_email_identifier_from_msg91_is_handled():
    with _mock_msg91_success("newuser@example.com"):
        r = client.post(VERIFY_URL, json={"access_token": "good-token"})

    assert r.status_code == 200
    body = r.json()
    assert body["user"]["email"] == "newuser@example.com"
    assert body["user"]["is_email_verified"] is True


# ---------------------------------------------------------------------------
# 8. Unparseable MSG91 identifier -> 502, not a 500/crash
# ---------------------------------------------------------------------------

def test_unrecognizable_identifier_returns_502():
    with _mock_msg91_success("not-a-phone-or-email"):
        r = client.post(VERIFY_URL, json={"access_token": "good-token"})

    assert r.status_code == 502
    assert "unrecognized identifier" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 9. Duplicate-account race and generic DB errors are handled cleanly
#    (requirement: "duplicate account" / "database errors" must not surface
#    as an unhandled 500).
# ---------------------------------------------------------------------------

def test_concurrent_duplicate_identifier_does_not_500():
    """
    Simulates two verify-msg91-token calls for the same brand-new phone
    number racing each other: the first call's user-creation commit wins,
    the second should hit the IntegrityError path and gracefully log the
    caller into the row the first request just created — never a raw 500.
    """
    import app.routers.auth_routes as auth_routes_module
    from app import models

    real_get_user = auth_routes_module._get_user_by_identifier
    call_count = {"n": 0}

    def racy_get_user(db, identifier, channel):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Our own lookup finds nothing yet, so we're about to take the
            # create path — but simulate a concurrent request winning the
            # race right now, before our own commit: insert the competing
            # row directly so our subsequent db.add()+db.commit() below
            # hits the real unique constraint on phone_number.
            other = models.User(username="racer_user", phone_number=identifier)
            db.add(other)
            db.commit()
            return None
        # Second call happens inside the IntegrityError recovery path —
        # use the real lookup so it finds the row the "other" request created.
        return real_get_user(db, identifier, channel)

    with _mock_msg91_success("919876500000"), patch.object(
        auth_routes_module, "_get_user_by_identifier", side_effect=racy_get_user
    ):
        r = client.post(VERIFY_URL, json={"access_token": "good-token"})

    assert r.status_code == 200, r.text
    assert r.json()["user"]["username"] == "racer_user"


def test_database_error_during_finalize_returns_503_not_500():
    import app.routers.auth_routes as auth_routes_module
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SQLASession

    def boom(*args, **kwargs):
        raise OperationalError("commit", {}, Exception("connection lost"))

    with _mock_msg91_success("919876500001"):
        with patch.object(SQLASession, "commit", side_effect=boom):
            r = client.post(VERIFY_URL, json={"access_token": "good-token"})

    assert r.status_code == 503
    assert "database error" in r.json()["detail"].lower()

    # No half-created user should be left behind after the rollback.
    db = TestSessionLocal()
    try:
        assert db.query(models.User).filter(
            models.User.phone_number == "+919876500001"
        ).count() == 0
    finally:
        db.close()
