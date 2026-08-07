import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from dotenv import load_dotenv
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from typing import Optional

from app.database import get_db
from app import models

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "30"))

OTP_LENGTH = int(os.getenv("OTP_LENGTH", "6"))
OTP_EXPIRE_MINUTES = int(os.getenv("OTP_EXPIRE_MINUTES", "5"))
OTP_RESEND_COOLDOWN_SECONDS = int(os.getenv("OTP_RESEND_COOLDOWN_SECONDS", "30"))
OTP_MAX_ATTEMPTS = int(os.getenv("OTP_MAX_ATTEMPTS", "5"))

# tokenUrl points at login since that's the primary way a client gets a token now
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login")
# auto_error=False lets public-but-personalizable endpoints (e.g. follower
# lists that show "is_following" when you're logged in) work with or
# without a token, instead of 401ing anonymous callers outright.
oauth2_scheme_optional = OAuth2PasswordBearer(tokenUrl="api/auth/login", auto_error=False)


# ---- OTP helpers ----
# OTPs are short-lived (minutes), numeric, and already rate-limited by
# OTP_MAX_ATTEMPTS + a resend cooldown, so a fast keyed hash (HMAC-SHA256)
# is a good fit here — it avoids bcrypt's 72-byte input quirks and
# passlib/bcrypt version incompatibilities that don't matter for this
# use case, while still keeping the raw OTP out of the database.

def generate_otp() -> str:
    """Cryptographically random numeric OTP, zero-padded to OTP_LENGTH digits."""
    upper_bound = 10**OTP_LENGTH
    return str(secrets.randbelow(upper_bound)).zfill(OTP_LENGTH)


def hash_otp(otp: str) -> str:
    return hmac.new(SECRET_KEY.encode(), otp.encode(), hashlib.sha256).hexdigest()


def verify_otp_hash(otp: str, otp_hash: str) -> bool:
    return hmac.compare_digest(hash_otp(otp), otp_hash)


def otp_expiry_time() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRE_MINUTES)


# ---- Password helpers ----
# Unlike OTPs, passwords are long-lived secrets a user reuses, so they get a
# slow, salted hash (bcrypt) instead of the fast keyed hash used for OTPs.

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed_password.encode())
    except ValueError:
        # Malformed/legacy hash — never crash the auth path on bad stored data.
        return False


# ---- JWT (access token) helpers ----

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> models.User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != "access":
            raise credentials_exception
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(models.User).filter(models.User.id == int(user_id)).first()
    if user is None or not user.is_active:
        raise credentials_exception
    return user


def get_current_user_optional(
    token: str | None = Depends(oauth2_scheme_optional), db: Session = Depends(get_db)
) -> Optional[models.User]:
    """Like get_current_user, but returns None instead of raising for missing/invalid tokens."""
    if token is None:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != "access":
            return None
        user_id = payload.get("sub")
        if user_id is None:
            return None
    except JWTError:
        return None

    user = db.query(models.User).filter(models.User.id == int(user_id)).first()
    if user is None or not user.is_active:
        return None
    return user


# ---- Refresh token helpers ----
# Refresh tokens are opaque random strings (not JWTs). Only their hash is
# stored, so a leaked database dump doesn't hand out valid refresh tokens —
# same reasoning as the OTP hash above, just with a slower-changing secret.

def _hash_refresh_token(raw_token: str) -> str:
    return hmac.new(SECRET_KEY.encode(), raw_token.encode(), hashlib.sha256).hexdigest()


def create_refresh_token(db: Session, user_id: int) -> str:
    raw_token = secrets.token_urlsafe(48)
    record = models.RefreshToken(
        user_id=user_id,
        token_hash=_hash_refresh_token(raw_token),
        expires_at=datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(record)
    db.commit()
    return raw_token


def get_valid_refresh_token(db: Session, raw_token: str) -> models.RefreshToken | None:
    token_hash = _hash_refresh_token(raw_token)
    record = (
        db.query(models.RefreshToken)
        .filter(models.RefreshToken.token_hash == token_hash)
        .first()
    )
    if record is None or record.revoked:
        return None

    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        return None

    return record


def revoke_refresh_token(db: Session, record: models.RefreshToken) -> None:
    record.revoked = True
    db.commit()
