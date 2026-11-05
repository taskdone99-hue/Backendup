from datetime import datetime, timedelta, timezone

import pytest

from app import auth, models
from app.routers import auth_routes


@pytest.fixture

def account_otp_delivery(monkeypatch):
    """Return OTPs in test responses and prevent external SMS/email delivery."""
    monkeypatch.setattr(auth_routes, "DEBUG_RETURN_OTP", True)
    monkeypatch.setattr(auth_routes, "send_otp_email", lambda *args, **kwargs: None)
    monkeypatch.setattr(auth_routes, "send_password_reset_email", lambda *args, **kwargs: None)
    monkeypatch.setattr(auth_routes, "send_otp_sms", lambda *args, **kwargs: None)


def test_change_email_requires_otp_and_marks_new_email_verified(
    client, db, make_user, account_otp_delivery
):
    user = make_user("email_change_owner")
    user.is_email_verified = True
    db.commit()
    client.login(user)

    requested = client.post(
        "/api/auth/change-email/request",
        json={"email": "new-address@example.test"},
    )
    assert requested.status_code == 200
    otp = requested.json()["debug_otp"]

    verified = client.post(
        "/api/auth/change-email/verify",
        json={"email": "new-address@example.test", "otp": otp},
    )
    assert verified.status_code == 200
    assert user.email == "new-address@example.test"
    assert user.is_email_verified is True


def test_change_phone_requires_otp_and_marks_new_phone_verified(
    client, db, make_user, account_otp_delivery
):
    user = make_user("phone_change_owner")
    client.login(user)

    requested = client.post(
        "/api/auth/change-phone/request",
        json={"phone_number": "+919876543210"},
    )
    assert requested.status_code == 200
    otp = requested.json()["debug_otp"]

    verified = client.post(
        "/api/auth/change-phone/verify",
        json={"phone_number": "+919876543210", "otp": otp},
    )
    assert verified.status_code == 200
    assert user.phone_number == "+919876543210"
    assert user.is_phone_verified is True


def test_change_contact_rejects_identifier_used_by_another_user(
    client, make_user
):
    owner = make_user("contact_owner")
    other = make_user("contact_other")
    client.login(owner)

    response = client.post(
        "/api/auth/change-email/request",
        json={"email": other.email},
    )
    assert response.status_code == 409


def test_change_password_verifies_current_password_and_revokes_refresh_tokens(
    client, db, make_user
):
    user = make_user("password_change_owner")
    user.hashed_password = auth.hash_password("OldPassword123")
    db.add(
        models.RefreshToken(
            user_id=user.id,
            token_hash="refresh-hash-before-password-change",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            revoked=False,
        )
    )
    db.commit()
    client.login(user)

    response = client.post(
        "/api/auth/change-password",
        json={"current_password": "OldPassword123", "new_password": "NewPassword456"},
    )
    assert response.status_code == 200
    assert auth.verify_password("NewPassword456", user.hashed_password)
    token = db.query(models.RefreshToken).filter_by(user_id=user.id).one()
    assert token.revoked is True


def test_delete_account_requires_verified_contact_otp_and_password(
    client, db, make_user, account_otp_delivery
):
    user = make_user("delete_account_owner")
    user.hashed_password = auth.hash_password("DeletePassword123")
    user.is_email_verified = True
    db.add(
        models.RefreshToken(
            user_id=user.id,
            token_hash="refresh-hash-before-delete",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            revoked=False,
        )
    )
    db.add(models.DeviceToken(user_id=user.id, token="device-token-before-delete"))
    db.commit()
    old_user_id = user.id
    old_email = user.email
    client.login(user)

    requested = client.post(
        "/api/auth/delete-account/request",
        json={"channel": "email"},
    )
    assert requested.status_code == 200
    otp = requested.json()["debug_otp"]

    bad_password = client.post(
        "/api/auth/delete-account/confirm",
        json={
            "identifier": old_email,
            "otp": otp,
            "confirmation": "DELETE",
            "current_password": "wrong-password",
        },
    )
    assert bad_password.status_code == 400
    assert user.is_active is True

    deleted = client.post(
        "/api/auth/delete-account/confirm",
        json={
            "identifier": old_email,
            "otp": otp,
            "confirmation": "DELETE",
            "current_password": "DeletePassword123",
        },
    )
    assert deleted.status_code == 200
    assert user.is_active is False
    assert user.email is None
    assert user.phone_number is None
    assert user.hashed_password is None
    assert user.username.startswith(f"deleted_{old_user_id}_")
    assert db.query(models.RefreshToken).filter_by(user_id=old_user_id).one().revoked is True
    assert db.query(models.DeviceToken).filter_by(user_id=old_user_id).count() == 0


def test_delete_account_passwordless_user_can_confirm_with_otp(
    client, db, make_user, account_otp_delivery
):
    user = make_user("otp_only_delete_owner")
    user.hashed_password = None
    user.is_phone_verified = True
    user.phone_number = "+919876543211"
    db.commit()
    client.login(user)

    requested = client.post(
        "/api/auth/delete-account/request",
        json={"channel": "phone"},
    )
    assert requested.status_code == 200
    otp = requested.json()["debug_otp"]

    deleted = client.post(
        "/api/auth/delete-account/confirm",
        json={
            "identifier": "+919876543211",
            "otp": otp,
            "confirmation": "DELETE",
        },
    )
    assert deleted.status_code == 200
    assert user.is_active is False


def test_generic_otp_endpoint_does_not_expose_account_change_purposes(client):
    response = client.post(
        "/api/auth/request-otp",
        json={"identifier": "someone@example.test", "purpose": "delete_account"},
    )
    assert response.status_code == 400
