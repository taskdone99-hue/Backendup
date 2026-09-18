"""
Regression tests for the "one active device/session per account" rule:
logging in while a session is already active is blocked (409); logging
out, or letting the session expire, frees the account up for a new login.

Covers the password-login endpoint directly. The OTP/MSG91 login paths
share the same underlying check (raise_if_active_session, called from
_finalize_verified_identifier) and are covered separately in
test/test_msg91_auth.py, which runs against a real JWT-issuing app
instead of this suite's stubbed-auth `client` fixture.
"""
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-single-session-tests")

from app import models
from app import auth
from app.auth import hash_password, _hash_refresh_token

auth.SECRET_KEY = os.environ["SECRET_KEY"]


PASSWORD = "correct horse battery staple"


def _make_password_user(db, username="loginuser"):
    user = models.User(
        username=username,
        email=f"{username}@example.test",
        hashed_password=hash_password(PASSWORD),
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_second_login_blocked_while_session_active(client, db):
    user = _make_password_user(db)

    first = client.post(
        "/api/auth/login",
        json={"identifier": user.username, "password": PASSWORD},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/api/auth/login",
        json={"identifier": user.username, "password": PASSWORD},
    )
    assert second.status_code == 409
    assert "already logged in" in second.json()["message"].lower()

    # Only one non-revoked session should exist for this account.
    active_sessions = (
        db.query(models.RefreshToken)
        .filter(models.RefreshToken.user_id == user.id, models.RefreshToken.revoked == False)
        .count()
    )
    assert active_sessions == 1


def test_login_allowed_after_logout(client, db):
    user = _make_password_user(db, "logoutuser")

    first = client.post(
        "/api/auth/login",
        json={"identifier": user.username, "password": PASSWORD},
    )
    assert first.status_code == 200

    logout = client.post(
        "/api/auth/logout",
        json={"refresh_token": first.json()["refresh_token"]},
    )
    assert logout.status_code == 200

    second = client.post(
        "/api/auth/login",
        json={"identifier": user.username, "password": PASSWORD},
    )
    assert second.status_code == 200
    assert second.json()["refresh_token"] != first.json()["refresh_token"]


def test_login_allowed_after_session_expiry(client, db):
    user = _make_password_user(db, "expireduser")

    first = client.post(
        "/api/auth/login",
        json={"identifier": user.username, "password": PASSWORD},
    )
    assert first.status_code == 200

    token_hash = _hash_refresh_token(first.json()["refresh_token"])
    record = (
        db.query(models.RefreshToken)
        .filter(models.RefreshToken.token_hash == token_hash)
        .first()
    )
    record.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()

    second = client.post(
        "/api/auth/login",
        json={"identifier": user.username, "password": PASSWORD},
    )
    assert second.status_code == 200


def test_different_accounts_are_not_blocked_by_each_other(client, db):
    user_a = _make_password_user(db, "usera")
    user_b = _make_password_user(db, "userb")

    a_login = client.post(
        "/api/auth/login",
        json={"identifier": user_a.username, "password": PASSWORD},
    )
    assert a_login.status_code == 200

    b_login = client.post(
        "/api/auth/login",
        json={"identifier": user_b.username, "password": PASSWORD},
    )
    assert b_login.status_code == 200


def test_refresh_token_endpoint_does_not_start_a_new_session(client, db):
    """Renewing the access token via /refresh-token reuses the existing
    session rather than creating a second one, so it must never itself
    trigger the single-session block on a later call."""
    user = _make_password_user(db, "refreshuser")

    login_resp = client.post(
        "/api/auth/login",
        json={"identifier": user.username, "password": PASSWORD},
    )
    assert login_resp.status_code == 200
    refresh_token = login_resp.json()["refresh_token"]

    refresh_resp = client.post(
        "/api/auth/refresh-token",
        json={"refresh_token": refresh_token},
    )
    assert refresh_resp.status_code == 200
    assert refresh_resp.json()["access_token"]

    # A second login attempt is still correctly blocked (one session, not
    # freshly duplicated by the refresh call).
    second_login = client.post(
        "/api/auth/login",
        json={"identifier": user.username, "password": PASSWORD},
    )
    assert second_login.status_code == 409

    active_sessions = (
        db.query(models.RefreshToken)
        .filter(models.RefreshToken.user_id == user.id, models.RefreshToken.revoked == False)
        .count()
    )
    assert active_sessions == 1


def test_password_reset_frees_up_a_new_login(client, db):
    """Existing behavior: resetting the password revokes all active
    refresh tokens. That must continue to free the account for a new
    login under the new single-session rule too."""
    user = _make_password_user(db, "resetuser")

    first = client.post(
        "/api/auth/login",
        json={"identifier": user.username, "password": PASSWORD},
    )
    assert first.status_code == 200

    # Revoke directly the way reset-password does, rather than exercising
    # the OTP flow, to keep this test focused on the session rule.
    db.query(models.RefreshToken).filter(
        models.RefreshToken.user_id == user.id, models.RefreshToken.revoked == False
    ).update({"revoked": True})
    db.commit()

    second = client.post(
        "/api/auth/login",
        json={"identifier": user.username, "password": PASSWORD},
    )
    assert second.status_code == 200
