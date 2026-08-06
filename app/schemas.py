import os
import re
from datetime import date, datetime

import phonenumbers
from pydantic import BaseModel, Field, field_validator, model_validator

from app.models import Gender, MediaType, OTPChannel, OTPPurpose

PASSWORD_MIN_LENGTH = int(os.getenv("PASSWORD_MIN_LENGTH", "8"))
# Matches Instagram's own minimum signup age.
MIN_SIGNUP_AGE_YEARS = int(os.getenv("MIN_SIGNUP_AGE_YEARS", "13"))

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Letters, numbers, periods, and underscores only; must start and end with a
# letter or number (no leading/trailing punctuation) — same shape as
# Instagram's own username rules.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._]{1,28}[A-Za-z0-9])?$")


def _normalize_phone(value: str) -> str:
    """Validate and normalize a phone number to E.164 format (e.g. +919876543210)."""
    try:
        parsed = phonenumbers.parse(value, None)
    except phonenumbers.NumberParseException:
        raise ValueError(
    "Please include your country code, e.g. +91 for India, +1 for the US, etc."
)
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError("This does not look like a valid phone number")

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def normalize_identifier(value: str) -> tuple[str, OTPChannel]:
    """Accepts an email address or a phone number and returns (normalized_value, channel)."""
    value = value.strip()
    if _EMAIL_RE.match(value):
        return value.lower(), OTPChannel.email
    return _normalize_phone(value), OTPChannel.phone


def _validate_password_strength(password: str) -> str:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters long")
    return password


def _validate_username_format(username: str) -> str:
    username = username.strip().lower()
    if not _USERNAME_RE.match(username):
        raise ValueError(
            "Username must be 3-30 characters, using only letters, numbers, "
            "periods, or underscores, and can't start or end with a period/underscore"
        )
    return username


def _validate_dob(dob: date) -> date:
    today = date.today()
    age_years = (
        today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    )
    if dob > today:
        raise ValueError("Date of birth can't be in the future")
    if age_years < MIN_SIGNUP_AGE_YEARS:
        raise ValueError(f"You must be at least {MIN_SIGNUP_AGE_YEARS} years old to sign up")
    return dob


# ---- OTP request/verify ----

class RequestOTPRequest(BaseModel):
    identifier: str = Field(description="Email address or E.164 phone number")
    purpose: OTPPurpose = OTPPurpose.signup

    @field_validator("identifier")
    @classmethod
    def validate_identifier(cls, v: str) -> str:
        normalized, _ = normalize_identifier(v)
        return normalized


class VerifyOTPRequest(BaseModel):
    identifier: str
    otp: str = Field(min_length=4, max_length=8)
    purpose: OTPPurpose = OTPPurpose.signup

    @field_validator("identifier")
    @classmethod
    def validate_identifier(cls, v: str) -> str:
        normalized, _ = normalize_identifier(v)
        return normalized


class OTPResponse(BaseModel):
    message: str
    identifier: str
    expires_in_seconds: int
    # Only populated when DEBUG_RETURN_OTP=true, for local testing without a
    # real SMS/email provider.
    debug_otp: str | None = None


# ---- Username availability ----

class CheckUsernameRequest(BaseModel):
    username: str

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        return _validate_username_format(v)


class CheckUsernameResponse(BaseModel):
    username: str
    available: bool
    message: str
    suggestions: list[str] | None = None

# ---- Register / Login ----

class RegisterRequest(BaseModel):
    """
    Mirrors an Instagram-style signup: pick a unique username, provide a
    phone number or email, choose a password, then confirm date of birth.
    Nothing is created yet — this just validates the data, stashes it as a
    pending signup, and sends an OTP. The account itself is only created
    once /verify-otp (purpose=signup) confirms the code.
    """
    username: str = Field(description="Unique username, 3-30 characters (letters, numbers, '.', '_')")
    identifier: str = Field(description="Email address or E.164 phone number")
    password: str
    date_of_birth: date
    gender: Gender | None = Field(
        default=None, description="Optional — male, female, non_binary, or prefer_not_to_say"
    )

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        return _validate_username_format(v)

    @field_validator("identifier")
    @classmethod
    def validate_identifier(cls, v: str) -> str:
        normalized, _ = normalize_identifier(v)
        return normalized

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return _validate_password_strength(v)

    @field_validator("date_of_birth")
    @classmethod
    def validate_dob(cls, v: date) -> date:
        return _validate_dob(v)


class LoginRequest(BaseModel):
    identifier: str
    password: str

    @field_validator("identifier")
    @classmethod
    def validate_identifier(cls, v: str) -> str:
        v = v.strip()

        # Email
        if _EMAIL_RE.match(v):
            return v.lower()

        # Phone number
        try:
            return _normalize_phone(v)
        except ValueError:
            pass

        # Username
        return _validate_username_format(v)

# ---- Refresh / Logout ----

class RefreshTokenRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


# ---- Forgot / Reset password ----

class ForgotPasswordRequest(BaseModel):
    identifier: str

    @field_validator("identifier")
    @classmethod
    def validate_identifier(cls, v: str) -> str:
        normalized, _ = normalize_identifier(v)
        return normalized


class ResetPasswordRequest(BaseModel):
    identifier: str
    otp: str = Field(min_length=4, max_length=8)
    new_password: str

    @field_validator("identifier")
    @classmethod
    def validate_identifier(cls, v: str) -> str:
        normalized, _ = normalize_identifier(v)
        return normalized

    @field_validator("new_password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return _validate_password_strength(v)


# ---- Responses ----

class UserOut(BaseModel):
    id: int
    username: str
    phone_number: str | None
    email: str | None
    date_of_birth: date | None
    gender: Gender | None
    is_phone_verified: bool
    is_email_verified: bool
    created_at: datetime

    class Config:
        from_attributes = True


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserOut


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MessageResponse(BaseModel):
    message: str


# ==========================================================================
# Profile / Follow / Posts / Reels / Stories
# ==========================================================================

# ---- User profile ----

class UserProfileOut(BaseModel):
    """Public-facing profile — what GET /api/users/:id and follower/following lists return."""
    id: int
    username: str
    full_name: str | None
    bio: str | None
    avatar_url: str | None
    is_private: bool
    is_phone_verified: bool
    is_email_verified: bool
    created_at: datetime

    class Config:
        from_attributes = True


class UserProfileUpdate(BaseModel):
    """PUT /api/users/:id — every field optional so callers can patch just what changed."""
    full_name: str | None = Field(default=None, max_length=100)
    bio: str | None = Field(default=None, max_length=150)
    gender: Gender | None = None
    is_private: bool | None = None

    @field_validator("full_name")
    @classmethod
    def strip_full_name(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else v

    @field_validator("bio")
    @classmethod
    def strip_bio(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else v


class AvatarUploadResponse(BaseModel):
    message: str
    avatar_url: str


class UserStatsOut(BaseModel):
    user_id: int
    posts_count: int
    followers_count: int
    following_count: int


class UserSummaryOut(BaseModel):
    """Compact user shape used inside lists — followers, following, suggested."""
    id: int
    username: str
    full_name: str | None
    avatar_url: str | None
    is_following: bool = False

    class Config:
        from_attributes = True


# ---- Follow system ----

class FollowStatusResponse(BaseModel):
    message: str
    following: bool


class PaginatedUsersResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[UserSummaryOut]


# ---- Posts / Reels / Saved ----

class PostOut(BaseModel):
    id: int
    user_id: int
    caption: str | None
    media_url: str
    media_type: MediaType
    created_at: datetime

    class Config:
        from_attributes = True


class ReelOut(BaseModel):
    id: int
    user_id: int
    caption: str | None
    video_url: str
    thumbnail_url: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class PaginatedPostsResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[PostOut]


class PaginatedReelsResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ReelOut]


# ---- Stories ----

class StoryOut(BaseModel):
    id: int
    user_id: int
    media_url: str
    media_type: MediaType
    caption: str | None
    created_at: datetime
    expires_at: datetime
    views_count: int = 0
    viewed_by_me: bool = False

    class Config:
        from_attributes = True


class StoryUserFeedOut(BaseModel):
    """One entry per followed user who has an active story, grouping their stories together."""
    user: UserSummaryOut
    stories: list[StoryOut]
    has_unseen: bool


class StoryFeedResponse(BaseModel):
    items: list[StoryUserFeedOut]


class StoryViewerOut(BaseModel):
    id: int
    username: str
    avatar_url: str | None
    viewed_at: datetime

    class Config:
        from_attributes = True


class StoryViewResponse(BaseModel):
    message: str
    views_count: int
