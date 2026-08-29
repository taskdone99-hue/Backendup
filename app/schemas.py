import os
import re
from datetime import date, datetime

import phonenumbers
from pydantic import BaseModel, Field, field_validator, model_validator

from app.models import (
    DevicePlatform,
    Gender,
    LikeTargetType,
    MediaType,
    MembershipInterval,
    MembershipStatus,
    NotificationType,
    OTPChannel,
    OTPPurpose,
    PaymentProvider,
    PaymentStatus,
    SavedItemType,
    ShareContentType,
    AccountType,
)

PASSWORD_MIN_LENGTH = int(os.getenv("PASSWORD_MIN_LENGTH", "8"))
# Matches Instagram's own minimum signup age.
MIN_SIGNUP_AGE_YEARS = int(os.getenv("MIN_SIGNUP_AGE_YEARS", "13"))
# Region assumed for phone numbers submitted without a country code / '+'
# prefix (e.g. "9876543210" instead of "+919876543210"). Numbers that DO
# include a country code still work exactly as before — this is only a
# fallback for numbers that don't.
DEFAULT_PHONE_REGION = os.getenv("DEFAULT_PHONE_REGION", "IN")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Letters, numbers, periods, and underscores only; must start and end with a
# letter or number (no leading/trailing punctuation) — same shape as
# Instagram's own username rules.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._]{1,28}[A-Za-z0-9])?$")


def _normalize_phone(value: str) -> str:
    """
    Validate and normalize a phone number to E.164 format (e.g. +919876543210).
    A country code is optional: if the number is given as a bare local
    number (no leading '+'), it's assumed to belong to DEFAULT_PHONE_REGION.
    A number that does include a country code/'+' is parsed as-is either way.
    """
    try:
        parsed = phonenumbers.parse(value, DEFAULT_PHONE_REGION)
    except phonenumbers.NumberParseException:
        raise ValueError("This does not look like a valid phone number")
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError("This does not look like a valid phone number")

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def normalize_identifier(value: str) -> tuple[str, OTPChannel]:
    """Accepts an email address or a phone number and returns (normalized_value, channel)."""
    value = value.strip()
    if not value:
        raise ValueError("Please enter your email or phone number")
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
        if not v:
            raise ValueError("Please enter your email, phone number, or username")

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
    gender: Gender | None
    avatar_url: str | None
    is_private: bool
    is_phone_verified: bool
    is_email_verified: bool
    account_type: AccountType = AccountType.personal
    business_name: str | None = None
    business_category: str | None = None
    business_description: str | None = None
    posts_count: int = 0
    reels_count: int = 0
    followers_count: int = 0
    following_count: int = 0
    is_following: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


class UserProfileUpdate(BaseModel):
    """PUT /api/users/:id — every field optional so callers can patch just what changed."""
    username: str | None = Field(
        default=None, description="Unique username, 3-30 characters (letters, numbers, '.', '_')"
    )
    full_name: str | None = Field(default=None, max_length=100)
    bio: str | None = Field(default=None, max_length=150)
    gender: Gender | None = None
    is_private: bool | None = None
    account_type: AccountType | None = None
    business_name: str | None = Field(default=None, max_length=100)
    business_category: str | None = Field(default=None, max_length=100)
    business_description: str | None = Field(default=None, max_length=500)

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str | None) -> str | None:
        return _validate_username_format(v) if v is not None else v

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
    reels_count: int
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
    # True when this created a pending request to a private account
    # instead of an immediate follow — additive field, false in every
    # case that behaves the way this endpoint always has.
    request_pending: bool = False


class FollowRequestOut(BaseModel):
    id: int
    requester: UserSummaryOut
    created_at: datetime

    class Config:
        from_attributes = True


class FollowRequestsResponse(BaseModel):
    items: list[FollowRequestOut]


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


# ---- Watch tracking (reel watch-time) ----

class WatchStartRequest(BaseModel):
    reel_id: int = Field(..., gt=0)


class WatchStartResponse(BaseModel):
    session_id: int
    reel_id: int
    started_at: datetime


class WatchEndRequest(BaseModel):
    session_id: int = Field(..., gt=0)
    # started_at/ended_at are deliberately NOT accepted from the client here —
    # trusting a client-supplied timestamp would let a modified app inflate
    # watch time. started_at was fixed at /watch/start and ended_at is always
    # the server's clock at the moment this request is handled.


class WatchEndResponse(BaseModel):
    session_id: int
    reel_id: int
    watch_seconds: int
    counted: bool  # False when under the "ignore short sessions" floor


class WatchHistoryItem(BaseModel):
    session_id: int
    reel_id: int
    started_at: datetime
    ended_at: datetime
    watch_seconds: int


class PaginatedWatchHistoryResponse(BaseModel):
    total: int
    limit: int
    offset: int
    # All-time totals across every valid session (not just this page) —
    # same numbers as GET /api/watch/stats -> total, included here too so a
    # profile screen can get the paginated list and the all-time total in
    # one call instead of two.
    total_watch_seconds: int = 0
    total_reels_watched: int = 0
    items: list[WatchHistoryItem]


class WatchPeriodStats(BaseModel):
    watch_seconds: int
    reels_watched: int


class WatchStatsResponse(BaseModel):
    today: WatchPeriodStats
    week: WatchPeriodStats
    month: WatchPeriodStats
    total: WatchPeriodStats


# ---- Stories ----

class StoryOut(BaseModel):
    id: int
    user_id: int
    user: UserSummaryOut | None = None
    media_url: str
    media_type: MediaType
    caption: str | None
    created_at: datetime
    expires_at: datetime
    views_count: int = 0
    viewed_by_me: bool = False
    reactions_count: int = 0
    my_reaction: str | None = None

    class Config:
        from_attributes = True


class StoryUserFeedOut(BaseModel):
    """One entry per followed user who has an active story, grouping their stories together."""
    user: UserSummaryOut
    stories: list[StoryOut]
    has_unseen: bool


class StoryFeedResponse(BaseModel):
    items: list[StoryUserFeedOut]


class MyStoriesResponse(BaseModel):
    items: list[StoryOut]


class StoryViewerOut(BaseModel):
    id: int
    user_id: int
    username: str
    full_name: str | None = None
    avatar_url: str | None
    viewed_at: datetime

    class Config:
        from_attributes = True


class StoryViewersResponse(BaseModel):
    views_count: int
    items: list[StoryViewerOut]


class StoryViewResponse(BaseModel):
    message: str
    views_count: int


# ---- Story reactions & replies ----

class StoryReactionCreate(BaseModel):
    emoji: str = Field(..., min_length=1, max_length=16)

    @field_validator("emoji")
    @classmethod
    def strip_emoji(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Reaction can't be empty")
        return v


class StoryReactorOut(BaseModel):
    id: int
    username: str
    avatar_url: str | None
    emoji: str
    created_at: datetime

    class Config:
        from_attributes = True


class StoryReactionsResponse(BaseModel):
    reactions_count: int
    items: list[StoryReactorOut]


class StoryReplyCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=2200)

    @field_validator("content")
    @classmethod
    def strip_content(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Reply can't be empty")
        return v


# ==========================================================================
# Posts (detail) / Reels (detail) / Comments / Likes / Video creation
# ==========================================================================

# ---- Posts ----

class MusicIn(BaseModel):
    title: str = Field(..., max_length=150)
    artist: str | None = Field(default=None, max_length=150)
    audio_url: str = Field(..., max_length=500)
    start_seconds: int = Field(default=0, ge=0)


class LocationIn(BaseModel):
    name: str = Field(..., max_length=150)
    latitude: float | None = None
    longitude: float | None = None


class PostUpdate(BaseModel):
    """
    PUT /api/posts/:id. Every field is optional and only touched if present
    in the request — omit a field to leave it unchanged, send it as `null`
    to clear it (e.g. `"music": null` removes the post's music).

    Swapping the actual image/video file is a separate call — see
    PUT /api/posts/:id/media — since that needs a multipart upload rather
    than JSON.
    """
    caption: str | None = Field(default=None, max_length=2200)
    music: MusicIn | None = None
    location: LocationIn | None = None
    alt_text: str | None = Field(default=None, max_length=1000)
    ai_generated: bool | None = None
    # Full replacement lists — send the complete set of user ids you want
    # tagged/added; anyone already tagged/added but missing from the list
    # gets removed. Omit the field entirely to leave tags/members untouched.
    tag_user_ids: list[int] | None = None
    member_user_ids: list[int] | None = None

    @field_validator("caption", "alt_text")
    @classmethod
    def strip_text(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else v


class MusicOut(BaseModel):
    title: str
    artist: str | None
    audio_url: str
    start_seconds: int


class LocationOut(BaseModel):
    name: str
    latitude: float | None
    longitude: float | None


class PostDetailOut(PostOut):
    user: UserSummaryOut | None = None
    likes_count: int = 0
    comments_count: int = 0
    share_count: int = 0
    hashtags: list[str] = []
    # Number of media items attached to the post. Posts currently store a
    # single `media_url` each (no carousel/multi-image support yet), so
    # this is always 1 — included now so the frontend has a stable field
    # to key off if/when carousel posts are added.
    media_count: int = 1
    is_liked: bool = False
    like_id: int | None = None
    is_saved: bool = False
    music: MusicOut | None = None
    location: LocationOut | None = None
    alt_text: str | None = None
    ai_generated: bool = False
    tags_count: int = 0
    members_count: int = 0
    tags: list[UserSummaryOut] = []
    members: list[UserSummaryOut] = []


class PaginatedPostDetailResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[PostDetailOut]


# ---- Post details: tag people, add music, add location, add members ----

class PostTagEntry(BaseModel):
    user_id: int = Field(..., gt=0)
    x_position: float | None = Field(default=None, ge=0, le=1, description="Normalized 0.0-1.0 horizontal position on the image")
    y_position: float | None = Field(default=None, ge=0, le=1, description="Normalized 0.0-1.0 vertical position on the image")


class TagPeopleRequest(BaseModel):
    tags: list[PostTagEntry] = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_unique(self) -> "TagPeopleRequest":
        user_ids = [t.user_id for t in self.tags]
        if len(user_ids) != len(set(user_ids)):
            raise ValueError("Each user can only be tagged once per request")
        return self


class PostTagOut(BaseModel):
    id: int
    user: UserSummaryOut
    x_position: float | None
    y_position: float | None
    tagged_at: datetime

    class Config:
        from_attributes = True


class PostTagsResponse(BaseModel):
    message: str
    tags: list[PostTagOut]


class PostMemberAddRequest(BaseModel):
    user_id: int = Field(..., gt=0)


class PostMemberOut(BaseModel):
    id: int
    user: UserSummaryOut
    added_at: datetime

    class Config:
        from_attributes = True


class PostMembersResponse(BaseModel):
    message: str
    members: list[PostMemberOut]


class MusicUpdateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=150)
    artist: str | None = Field(default=None, max_length=150)
    audio_url: str = Field(..., min_length=1, max_length=500)
    start_seconds: int = Field(default=0, ge=0, description="Where in the track playback should start")

    @field_validator("title", "artist")
    @classmethod
    def strip_text(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else v


class MusicResponse(BaseModel):
    message: str
    music: MusicOut


class LocationUpdateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Location name can't be empty")
        return v


class LocationResponse(BaseModel):
    message: str
    location: LocationOut


# ---- Reels / Video ----

class ReelDetailOut(ReelOut):
    title: str | None = None
    remixed_from_id: int | None = None
    user: UserSummaryOut | None = None
    likes_count: int = 0
    comments_count: int = 0
    is_liked: bool = False
    like_id: int | None = None
    is_saved: bool = False


class PaginatedReelDetailResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ReelDetailOut]


class VideoMetadataUpdate(BaseModel):
    """PUT /api/videos/:id/metadata — both fields optional so callers can patch just one."""
    title: str | None = Field(default=None, max_length=150)
    description: str | None = Field(default=None, max_length=2200, description="Stored as the reel's caption")

    @field_validator("title")
    @classmethod
    def strip_title(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else v

    @field_validator("description")
    @classmethod
    def strip_description(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else v


class ThumbnailUploadResponse(BaseModel):
    message: str
    thumbnail_url: str


class CollaboratorAddRequest(BaseModel):
    user_id: int = Field(..., gt=0)


class CollaboratorOut(BaseModel):
    id: int
    user: UserSummaryOut
    added_at: datetime

    class Config:
        from_attributes = True


class CollaboratorsResponse(BaseModel):
    message: str
    collaborators: list[CollaboratorOut]


class RevenueShareEntry(BaseModel):
    user_id: int = Field(..., gt=0)
    percentage: int = Field(..., ge=0, le=100)


class RevenueSplitUpdateRequest(BaseModel):
    """
    PUT /api/videos/:id/revenue-split — replaces the entire split in one call.
    Every entry must be the video's creator or an already-tagged collaborator,
    and percentages must add up to exactly 100.
    """
    splits: list[RevenueShareEntry] = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_splits(self) -> "RevenueSplitUpdateRequest":
        user_ids = [s.user_id for s in self.splits]
        if len(user_ids) != len(set(user_ids)):
            raise ValueError("Each user can only appear once in the revenue split")
        total = sum(s.percentage for s in self.splits)
        if total != 100:
            raise ValueError(f"Revenue split percentages must add up to 100 (got {total})")
        return self


class RevenueShareOut(BaseModel):
    user_id: int
    percentage: int


class RevenueSplitResponse(BaseModel):
    message: str
    splits: list[RevenueShareOut]


# ---- Comments ----

class CommentCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=2200)

    @field_validator("content")
    @classmethod
    def strip_content(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Comment can't be empty")
        return v


class CommentOut(BaseModel):
    id: int
    post_id: int | None = None
    reel_id: int | None = None
    user_id: int
    user: UserSummaryOut | None = None
    parent_id: int | None
    content: str
    created_at: datetime
    likes_count: int = 0
    replies_count: int = 0
    is_liked: bool = False
    like_id: int | None = None

    class Config:
        from_attributes = True


class PaginatedCommentsResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[CommentOut]


# ---- Likes ----

class LikeCreate(BaseModel):
    target_type: LikeTargetType
    target_id: int = Field(..., gt=0)


class LikeOut(BaseModel):
    id: int
    user_id: int
    user: UserSummaryOut | None = None
    target_type: LikeTargetType
    target_id: int
    created_at: datetime

    class Config:
        from_attributes = True


class LikeActionResponse(BaseModel):
    message: str
    like: LikeOut | None = None
    likes_count: int


class PaginatedLikesResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[UserSummaryOut]


# ==========================================================================
# Story Highlights
# ==========================================================================

class HighlightCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=50)
    cover_url: str | None = None
    story_ids: list[int] = Field(
        default_factory=list, description="Active story ids (owned by the caller) to seed the highlight with"
    )

    @field_validator("title")
    @classmethod
    def strip_title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Title can't be empty")
        return v


class HighlightUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=50)
    cover_url: str | None = None

    @field_validator("title")
    @classmethod
    def strip_title(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("Title can't be empty")
        return v


class AddHighlightStoriesRequest(BaseModel):
    story_ids: list[int] = Field(..., min_length=1)


class HighlightItemOut(BaseModel):
    id: int
    media_url: str
    media_type: MediaType
    caption: str | None
    source_story_id: int | None
    added_at: datetime

    class Config:
        from_attributes = True


class HighlightOut(BaseModel):
    id: int
    user_id: int
    title: str
    cover_url: str | None
    created_at: datetime
    items_count: int = 0

    class Config:
        from_attributes = True


class HighlightDetailOut(HighlightOut):
    items: list[HighlightItemOut] = Field(default_factory=list)


class HighlightsListResponse(BaseModel):
    items: list[HighlightOut]


# ==========================================================================
# Saved Posts
# ==========================================================================

class SavePostRequest(BaseModel):
    post_id: int = Field(..., gt=0)


# ---- Saved (generalized: posts / reels / audio / series / collections) ----

class AudioOut(BaseModel):
    id: int
    title: str
    artist: str | None
    audio_url: str

    class Config:
        from_attributes = True


class SeriesOut(BaseModel):
    id: int
    user_id: int
    title: str
    cover_url: str | None
    reels_count: int = 0
    created_at: datetime

    class Config:
        from_attributes = True


class SeriesCreate(BaseModel):
    title: str = Field(..., max_length=150)
    cover_url: str | None = None
    reel_ids: list[int] = Field(default_factory=list)


class SavedItemOut(BaseModel):
    """One row in the Saved tab — exactly one of post/reel/audio/series is
    set, matching `type`."""
    type: SavedItemType
    saved_at: datetime
    post: PostDetailOut | None = None
    reel: ReelDetailOut | None = None
    audio: AudioOut | None = None
    series: SeriesOut | None = None


class PaginatedSavedResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[SavedItemOut]


class SaveItemRequest(BaseModel):
    target_type: SavedItemType
    target_id: int = Field(..., gt=0)


class SavedCollectionOut(BaseModel):
    id: int
    user_id: int
    name: str
    cover_url: str | None
    items_count: int = 0
    created_at: datetime

    class Config:
        from_attributes = True


class SavedCollectionCreate(BaseModel):
    name: str = Field(..., max_length=100)
    cover_url: str | None = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Collection name can't be empty")
        return v


class SavedCollectionsResponse(BaseModel):
    items: list[SavedCollectionOut]


class AddToCollectionRequest(BaseModel):
    target_type: SavedItemType
    target_id: int = Field(..., gt=0)


# ==========================================================================
# Share
# ==========================================================================

class InternalShareRequest(BaseModel):
    content_type: ShareContentType
    content_id: int = Field(..., gt=0)
    recipient_ids: list[int] = Field(..., min_length=1, description="User ids to share with")
    message: str | None = Field(default=None, max_length=500)

    @field_validator("recipient_ids")
    @classmethod
    def dedupe_recipients(cls, v: list[int]) -> list[int]:
        deduped = list(dict.fromkeys(v))
        if not deduped:
            raise ValueError("At least one recipient is required")
        return deduped

    @field_validator("message")
    @classmethod
    def strip_message(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else v


class ShareOut(BaseModel):
    id: int
    sender_id: int
    recipient_id: int
    content_type: ShareContentType
    content_id: int
    message: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class InternalShareResponse(BaseModel):
    message: str
    shares: list[ShareOut]


class ShareLinkResponse(BaseModel):
    post_id: int
    url: str


# ==========================================================================
# Snap / Camera & Filters
# ==========================================================================

class FilterOut(BaseModel):
    id: str
    name: str
    thumbnail_url: str | None = None
    category: str | None = None


class FiltersResponse(BaseModel):
    items: list[FilterOut]


class SnapOut(BaseModel):
    id: int
    user_id: int
    media_url: str
    media_type: MediaType
    filter_id: str | None
    caption: str | None
    created_at: datetime

    class Config:
        from_attributes = True


# ==========================================================================
# Chat
# ==========================================================================

class ConversationCreate(BaseModel):
    participant_ids: list[int] = Field(
        ..., min_length=1, description="Other user ids in the conversation (caller is added automatically)"
    )
    title: str | None = Field(default=None, max_length=100, description="Group conversation name")

    @field_validator("participant_ids")
    @classmethod
    def dedupe_participants(cls, v: list[int]) -> list[int]:
        deduped = list(dict.fromkeys(v))
        if not deduped:
            raise ValueError("At least one other participant is required")
        return deduped


class MessageCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=2200)

    @field_validator("content")
    @classmethod
    def strip_content(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Message can't be empty")
        return v


class MessageOut(BaseModel):
    id: int
    conversation_id: int
    sender_id: int
    content: str
    reply_to_story_id: int | None = None
    is_auto_message: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


class ChatParticipantOut(BaseModel):
    id: int
    username: str
    full_name: str | None
    avatar_url: str | None
    is_online: bool = False


class PaginatedMessagesResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[MessageOut]


class ConversationOut(BaseModel):
    id: int
    is_group: bool
    title: str | None
    created_at: datetime
    participants: list[ChatParticipantOut]
    last_message: MessageOut | None = None
    unread_count: int = 0
    # Only meaningful from POST /api/chat/conversations: whether this call
    # just created the 1:1 thread (vs. returning an existing one), and — if
    # so — the auto-intro DM that was sent on B's behalf. Both are None on
    # every other endpoint that returns a ConversationOut (GET /conversations
    # etc), since they're only relevant at the moment of creation.
    is_new_conversation: bool | None = None
    profile_message: "ProfileMessageOut | None" = None


class ProfileMessageOut(BaseModel):
    message: str
    profile_id: int
    account_type: AccountType


class ConversationsResponse(BaseModel):
    items: list[ConversationOut]


class ChatFontUpdateRequest(BaseModel):
    font: str = Field(..., min_length=1, max_length=50)


class ChatFontResponse(BaseModel):
    message: str
    font: str


class MarkReadResponse(BaseModel):
    message: str
    last_read_message_id: int | None


class OnlineStatusOut(BaseModel):
    user_id: int
    is_online: bool


# ==========================================================================
# Notifications
# ==========================================================================

class NotificationOut(BaseModel):
    id: int
    type: NotificationType
    actor_id: int | None
    message: str
    target_type: str | None
    target_id: int | None
    is_read: bool
    created_at: datetime

    class Config:
        from_attributes = True


class PaginatedNotificationsResponse(BaseModel):
    total: int
    unread_count: int
    limit: int
    offset: int
    items: list[NotificationOut]


class NotificationReadResponse(BaseModel):
    message: str
    notification: NotificationOut


class DeviceTokenRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=255)
    platform: DevicePlatform | None = None


class DeviceTokenResponse(BaseModel):
    message: str
    token: str
    platform: DevicePlatform | None

# ==========================================================================
# Membership & Payments
# ==========================================================================

class MembershipPlanOut(BaseModel):
    id: int
    name: str
    description: str | None
    price_amount: int
    currency: str
    interval: MembershipInterval
    is_active: bool

    class Config:
        from_attributes = True


class MembershipPlansResponse(BaseModel):
    plans: list[MembershipPlanOut]


class SubscribeRequest(BaseModel):
    plan_id: int
    # Optional: ties this subscription to a payment that was already
    # confirmed paid via POST /api/payments/create-order + webhook.
    payment_order_id: int | None = None


class MembershipOut(BaseModel):
    id: int
    plan: MembershipPlanOut
    status: MembershipStatus
    current_period_start: datetime
    current_period_end: datetime | None

    class Config:
        from_attributes = True


class SubscribeResponse(BaseModel):
    message: str
    membership: MembershipOut


class MembershipStatusResponse(BaseModel):
    is_member: bool
    membership: MembershipOut | None


class CreateOrderRequest(BaseModel):
    plan_id: int
    provider: PaymentProvider = PaymentProvider.razorpay


class CreateOrderResponse(BaseModel):
    order_id: str
    amount: int
    currency: str
    provider: PaymentProvider
    # Public key the client SDK needs to open the provider's checkout —
    # e.g. RAZORPAY_KEY_ID / STRIPE_PUBLISHABLE_KEY. Null when the server
    # has no provider keys configured (dev/log mode).
    provider_key: str | None
    status: PaymentStatus


class PaymentWebhookResponse(BaseModel):
    message: str
    order_id: str | None = None
    status: PaymentStatus | None = None


# ==========================================================================
# Discord Integration
# ==========================================================================

class DiscordServerStatsOut(BaseModel):
    guild_id: str | None
    name: str | None
    member_count: int | None
    online_count: int | None
    invite_url: str | None
    # True when these numbers came live from Discord's widget API; false
    # when DISCORD_GUILD_ID isn't configured or the widget is unreachable.
    live: bool


class DiscordLinkRequest(BaseModel):
    discord_user_id: str = Field(..., min_length=1, max_length=32)
    discord_username: str | None = Field(default=None, max_length=100)


class DiscordLinkResponse(BaseModel):
    message: str
    discord_user_id: str
    discord_username: str | None
    linked_at: datetime


class DiscordWebhookResponse(BaseModel):
    message: str
    event: str | None = None


# ==========================================================================
# Ads
# ==========================================================================

class AdImpressionRequest(BaseModel):
    ad_id: str = Field(..., min_length=1, max_length=100)
    placement: str | None = Field(default=None, max_length=50)


class AdImpressionResponse(BaseModel):
    message: str
    ad_id: str
    placement: str | None


class AdSlotOut(BaseModel):
    placement: str
    enabled: bool
    frequency: int  # show one ad every N feed/reel items in this placement


class AdConfigResponse(BaseModel):
    ad_network: str | None
    test_mode: bool
    slots: list[AdSlotOut]
