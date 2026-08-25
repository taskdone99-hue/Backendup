import os
import re

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client
from fastapi import HTTPException
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.models import OTPChannel, OTPPurpose
from app.auth import (
    generate_otp,
    hash_otp,
    verify_otp_hash,
    otp_expiry_time,
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    get_valid_refresh_token,
    revoke_refresh_token,
    get_current_user,
    OTP_EXPIRE_MINUTES,
    OTP_RESEND_COOLDOWN_SECONDS,
    OTP_MAX_ATTEMPTS,
)
from app.services.sms_service import (
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_VERIFY_SERVICE_SID,
    is_twilio_configured,
    send_otp_sms,
)
from app.services.email_service import send_otp_email, send_password_reset_email

router = APIRouter(prefix="/api/auth", tags=["auth"])

DEBUG_RETURN_OTP = os.getenv("DEBUG_RETURN_OTP", "false").lower() == "true"


# ---- internal helpers ----

def _get_user_by_identifier(db: Session, identifier: str, channel: OTPChannel):
    if channel == OTPChannel.email:
        return db.query(models.User).filter(models.User.email == identifier).first()
    return db.query(models.User).filter(models.User.phone_number == identifier).first()


def _username_taken(db: Session, username: str) -> bool:
    return db.query(models.User).filter(models.User.username == username).first() is not None


def _generate_placeholder_username(db: Session, identifier: str) -> str:
    """
    Only used for the passwordless OTP-only path (verify-otp for an identifier
    that has never registered a username). Derives a starting point from the
    identifier, then appends a random suffix until it's unique — the user can
    change it later.
    """
    base = re.sub(r"[^a-z0-9]", "", identifier.split("@")[0].lower()) or "user"
    base = base[:20]
    candidate = base
    while _username_taken(db, candidate):
        candidate = f"{base}_{secrets.token_hex(3)}"
    return candidate


def _issue_otp(db: Session, identifier: str, channel: OTPChannel, purpose: OTPPurpose) -> str:
    """Creates and sends a fresh OTP, enforcing the resend cooldown. Returns the raw OTP."""
    now = datetime.now(timezone.utc)

    recent = (
        db.query(models.OTP)
        .filter(models.OTP.identifier == identifier, models.OTP.purpose == purpose)
        .order_by(models.OTP.created_at.desc())
        .first()
    )
    if recent is not None:
        recent_created = recent.created_at
        if recent_created.tzinfo is None:
            recent_created = recent_created.replace(tzinfo=timezone.utc)
        seconds_since = (now - recent_created).total_seconds()
        if seconds_since < OTP_RESEND_COOLDOWN_SECONDS:
            wait_for = int(OTP_RESEND_COOLDOWN_SECONDS - seconds_since)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Please wait {wait_for}s before requesting another code",
            )

    # Invalidate any previous unused OTPs for this identifier + purpose
    db.query(models.OTP).filter(
        models.OTP.identifier == identifier,
        models.OTP.purpose == purpose,
        models.OTP.is_used == False,
    ).update({"is_used": True})

    otp = generate_otp()
    db.add(
        models.OTP(
            identifier=identifier,
            channel=channel,
            purpose=purpose,
            otp_hash=hash_otp(otp),
            expires_at=otp_expiry_time(),
        )
    )
    db.commit()

    if channel == OTPChannel.email:
        if purpose == OTPPurpose.reset_password:
            send_password_reset_email(identifier, otp)
        else:
            send_otp_email(identifier, otp)
    else:
        send_otp_sms(identifier, otp)

    return otp


def _consume_otp(db: Session, identifier: str, purpose: OTPPurpose, otp: str) -> models.OTP:
    """Validates and marks an OTP as used, raising HTTPException on any failure."""
    now = datetime.now(timezone.utc)

    otp_record = (
        db.query(models.OTP)
        .filter(
            models.OTP.identifier == identifier,
            models.OTP.purpose == purpose,
            models.OTP.is_used == False,
        )
        .order_by(models.OTP.created_at.desc())
        .first()
    )

    if otp_record is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No active code for this request. Request a new one.",
        )

    expires_at = otp_record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if now > expires_at:
        otp_record.is_used = True
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Code has expired. Request a new one.",
        )

    if otp_record.attempts >= OTP_MAX_ATTEMPTS:
        otp_record.is_used = True
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many incorrect attempts. Request a new code.",
        )

    if not verify_otp_hash(otp, otp_record.otp_hash):
        otp_record.attempts += 1
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect code",
        )

    otp_record.is_used = True
    db.commit()
    return otp_record


def _issue_token_pair(db: Session, user: models.User) -> schemas.TokenResponse:
    access_token = create_access_token(data={"sub": str(user.id)})
    refresh_token = create_refresh_token(db, user.id)
    return schemas.TokenResponse(
        access_token=access_token, refresh_token=refresh_token, user=user
    )
import random

def _generate_username_suggestions(db: Session, base: str, count: int = 4) -> list[str]:
    suggestions = []
    candidates = [
        f"{base}{random.randint(10, 999)}",
        f"{base}_{random.randint(1, 99)}",
        f"the_{base}",
        f"{base}_official",
        f"real_{base}",
        f"{base}{random.randint(1000, 9999)}",
    ]
    for candidate in candidates:
        if len(suggestions) >= count:
            break
        if not _username_taken(db, candidate):
            suggestions.append(candidate)
    return suggestions
# ---- Username availability -
@router.post("/check-username", response_model=schemas.CheckUsernameResponse)
def check_username(payload: schemas.CheckUsernameRequest, db: Session = Depends(get_db)):
    taken = _username_taken(db, payload.username)
    suggestions = _generate_username_suggestions(db, payload.username) if taken else None
    return schemas.CheckUsernameResponse(
        username=payload.username,
        available=not taken,
        message="Username is available" if not taken else "Username is already taken",
        suggestions=suggestions,
    ) 
# ---- Register (steps 1-4: username -> identifier -> password -> DOB) ----

@router.post("/register", response_model=schemas.OTPResponse, status_code=status.HTTP_201_CREATED)
def register(payload: schemas.RegisterRequest, db: Session = Depends(get_db)):
    identifier, channel = schemas.normalize_identifier(payload.identifier)

    existing = _get_user_by_identifier(db, identifier, channel)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email/phone number already exists",
        )

    if _username_taken(db, payload.username):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username is already taken",
        )

    # Store (or overwrite) the pending signup — the real account isn't
    # created until the OTP below is verified.
    pending = (
        db.query(models.PendingSignup)
        .filter(models.PendingSignup.identifier == identifier)
        .first()
    )
    if pending is None:
        pending = models.PendingSignup(identifier=identifier)
        db.add(pending)

    pending.channel = channel
    pending.username = payload.username
    pending.hashed_password = hash_password(payload.password)
    pending.date_of_birth = payload.date_of_birth
    pending.gender = payload.gender
    db.commit()

    otp = _issue_otp(db, identifier, channel, OTPPurpose.signup)

    return schemas.OTPResponse(
        message="Enter the code we sent to finish creating your account",
        identifier=identifier,
        expires_in_seconds=OTP_EXPIRE_MINUTES * 60,
        debug_otp=otp if DEBUG_RETURN_OTP else None,
    )


# ---- Login ----
# ---- Login ----

@router.post("/login", response_model=schemas.TokenResponse)
def login(payload: schemas.LoginRequest, db: Session = Depends(get_db)):
    identifier = payload.identifier.strip()

    # Try email or phone first
    try:
        normalized, channel = schemas.normalize_identifier(identifier)
        user = _get_user_by_identifier(db, normalized, channel)
    except ValueError:
        # If it's not an email or phone, treat it as a username
        user = (
            db.query(models.User)
            .filter(models.User.username == identifier)
            .first()
        )

    invalid_credentials = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect username/email/phone number or password",
    )

    if user is None or not user.hashed_password:
        raise invalid_credentials

    if not verify_password(payload.password, user.hashed_password):
        raise invalid_credentials

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been deactivated",
        )

    return _issue_token_pair(db, user)


# ---- Refresh token ----

@router.post("/refresh-token", response_model=schemas.AccessTokenResponse)
def refresh_token(payload: schemas.RefreshTokenRequest, db: Session = Depends(get_db)):
    record = get_valid_refresh_token(db, payload.refresh_token)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    user = db.query(models.User).filter(models.User.id == record.user_id).first()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token"
        )

    access_token = create_access_token(data={"sub": str(user.id)})
    return schemas.AccessTokenResponse(access_token=access_token)


# ---- Logout ----

@router.post("/logout", response_model=schemas.MessageResponse)
def logout(payload: schemas.LogoutRequest, db: Session = Depends(get_db)):
    record = get_valid_refresh_token(db, payload.refresh_token)
    if record is not None:
        revoke_refresh_token(db, record)
    # Always respond success — logout is idempotent and shouldn't leak whether
    # the token existed.
    return schemas.MessageResponse(message="Logged out successfully")


# ---- Forgot / Reset password ----

@router.post("/forgot-password", response_model=schemas.OTPResponse)
def forgot_password(payload: schemas.ForgotPasswordRequest, db: Session = Depends(get_db)):
    identifier, channel = schemas.normalize_identifier(payload.identifier)
    user = _get_user_by_identifier(db, identifier, channel)

    # Always return a generic success response, whether or not the account
    # exists, so this endpoint can't be used to enumerate registered accounts.
    if user is not None:
        otp = None
        try:
            otp = _issue_otp(db, identifier, channel, OTPPurpose.reset_password)
        except HTTPException:
            pass
        debug_otp = otp if (DEBUG_RETURN_OTP and otp) else None
    else:
        debug_otp = None

    return schemas.OTPResponse(
        message="If an account exists for this email/phone number, a reset code has been sent",
        identifier=identifier,
        expires_in_seconds=OTP_EXPIRE_MINUTES * 60,
        debug_otp=debug_otp,
    )


@router.post("/reset-password", response_model=schemas.MessageResponse)
def reset_password(payload: schemas.ResetPasswordRequest, db: Session = Depends(get_db)):
    identifier, channel = schemas.normalize_identifier(payload.identifier)

    # Validate the OTP before checking the user, so response timing/behavior
    # doesn't reveal whether an account exists for this identifier.
    _consume_otp(db, identifier, OTPPurpose.reset_password, payload.otp)

    user = _get_user_by_identifier(db, identifier, channel)
    if user is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Incorrect code")

    user.hashed_password = hash_password(payload.new_password)
    db.commit()

    # Reset revokes all existing refresh tokens, so anyone with an old session
    # is signed out once the password changes.
    db.query(models.RefreshToken).filter(
        models.RefreshToken.user_id == user.id, models.RefreshToken.revoked == False
    ).update({"revoked": True})
    db.commit()

    return schemas.MessageResponse(message="Password reset successfully")


# ---- OTP request / verify (email or phone; signup, login, or verification) ----

@router.post("/request-otp", response_model=schemas.OTPResponse)
def request_otp(payload: schemas.RequestOTPRequest, db: Session = Depends(get_db)):
    identifier, channel = schemas.normalize_identifier(payload.identifier)
    otp = _issue_otp(db, identifier, channel, payload.purpose)

    return schemas.OTPResponse(
        message="OTP sent successfully",
        identifier=identifier,
        expires_in_seconds=OTP_EXPIRE_MINUTES * 60,
        debug_otp=otp if DEBUG_RETURN_OTP else None,
    )


@router.post("/verify-otp", response_model=schemas.TokenResponse)
def verify_otp(
    payload: schemas.VerifyOTPRequest,
    db: Session = Depends(get_db)
):
    identifier, channel = schemas.normalize_identifier(payload.identifier)

    if channel == OTPChannel.phone and is_twilio_configured():
        # Twilio Verify is authoritative for phone once configured — it
        # generated and sent its own code (see sms_service.send_otp_sms),
        # so it has to be the one that checks it too.
        try:
            client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            verification_check = (
                client.verify.v2
                .services(TWILIO_VERIFY_SERVICE_SID)
                .verification_checks
                .create(to=identifier, code=payload.otp)
            )
        except TwilioRestException as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Couldn't verify the code right now — please try again shortly.",
            ) from e

        if verification_check.status != "approved":
            raise HTTPException(
                status_code=400,
                detail="Invalid or expired OTP"
            )

    else:
        # Email always uses this; phone falls back to it too when Twilio
        # Verify isn't configured (local dev/demo — matches the console-log
        # fallback in sms_service.send_otp_sms).
        _consume_otp(
            db,
            identifier,
            payload.purpose,
            payload.otp
        )

    user = _get_user_by_identifier(
        db,
        identifier,
        channel
    )

    if user is None:
        pending = (
            db.query(models.PendingSignup)
            .filter(
                models.PendingSignup.identifier == identifier
            )
            .first()
        )

        if pending is not None:
            username = pending.username

            if _username_taken(db, username):
                username = _generate_placeholder_username(
                    db,
                    username
                )

            user = models.User(
                username=username,
                hashed_password=pending.hashed_password,
                date_of_birth=pending.date_of_birth,
                gender=pending.gender,
            )

            db.delete(pending)

        else:
            user = models.User(
                username=_generate_placeholder_username(
                    db,
                    identifier
                )
            )

        if channel == OTPChannel.email:
            user.email = identifier
        else:
            user.phone_number = identifier

        db.add(user)
        db.commit()
        db.refresh(user)

    if channel == OTPChannel.email:
        user.is_email_verified = True
    else:
        user.is_phone_verified = True

    db.commit()
    db.refresh(user)

    return _issue_token_pair(db, user)


# ---- Current user ----

@router.get("/me", response_model=schemas.UserOut)
def read_current_user(current_user: models.User = Depends(get_current_user)):
    return current_user
