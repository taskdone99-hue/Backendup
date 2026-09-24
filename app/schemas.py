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
    EarningSourceType,
    CollaborationStatus,
    BrandCollaborationStatus,
    LiveStatus,
    ReportTargetType,
    ReportReason,
    ReportStatus,
    ReportAction,
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


# ---- MSG91 OTP Widget verification ----

class MSG91VerifyTokenRequest(BaseModel):
    """
    Sent by the frontend once the MSG91 OTP Widget has completed its own
    client-side send-OTP/verify-OTP flow and returned a short-lived
    access-token. The backend never sees the OTP itself — only this
    token, which it exchanges with MSG91's verifyAccessToken API for the
    verified phone number (or email) before issuing our own JWT.
    """
    access_token: str = Field(
        min_length=1,
        description="Access token returned by the MSG91 OTP Widget after client-side verification",
    )


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
    # Both only meaningful relative to the logged-in viewer (false/false for
    # an anonymous request, or when viewing your own profile).
    is_followed_by: bool = False  # this user follows the viewer back
    request_pending: bool = False  # viewer has a follow request awaiting this user's response
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
    # Only populated where the caller needs the reverse direction too (e.g.
    # the follow-requests inbox, for Follow/Follow-Back/Following states).
    # Defaults to False everywhere else — unchanged behavior for existing
    # callers of this shape.
    is_followed_by: bool = False

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


class MonetizationStatusOut(BaseModel):
    monetization_enabled: bool
    watch_time_seconds: int
    required_watch_time_seconds: int
    remaining_seconds: int


class LocationIn(BaseModel):
    """Inline location payload for PUT /api/posts/:id's `location` field —
    kept name-only + optional coordinates for backward compatibility with
    existing callers of that endpoint. To attach a richer location (address,
    city, state, country, place_id) use POST /api/locations first and pass
    its id, or use the location_* fields on post/story creation."""
    name: str = Field(..., max_length=150)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class LocationCreate(BaseModel):
    """POST /api/locations — create (or reuse, if it matches an existing
    one) a saved location that can then be attached to posts/stories by id."""
    name: str = Field(..., max_length=150)
    address: str | None = Field(default=None, max_length=500)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, max_length=100)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    place_id: str | None = Field(default=None, max_length=255)


class LocationOut(BaseModel):
    """Additive vs. the original (name/latitude/longitude only) shape —
    id/address/city/state/country/place_id are new fields; every existing
    consumer of the old shape still gets name/latitude/longitude unchanged."""
    id: int | None = None
    name: str
    address: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    latitude: float | None
    longitude: float | None
    place_id: str | None = None

    class Config:
        from_attributes = True


class PaginatedLocationSearchResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[LocationOut]


class PlaceOut(BaseModel):
    """A real-world place from the external places/geocoding provider — used
    by GET /api/locations/places/search and GET /api/locations/reverse-geocode.
    The name/address/city/state/country/latitude/longitude/place_id fields
    line up with LocationCreate, so a client can pass a result straight to
    POST /api/locations (or the location_* fields on post/story creation).
    `place_id` is namespaced by provider ("google:...", "osm:N123")."""
    place_id: str | None = None
    name: str
    address: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    provider: str


class PlaceSearchResponse(BaseModel):
    items: list[PlaceOut]


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
    location: LocationOut | None = None
    mentions: "list[StoryMentionOut]" = []
    poll: "StoryPollOut | None" = None
    question: "StoryQuestionOut | None" = None
    close_friends_only: bool = False

    class Config:
        from_attributes = True


class PaginatedStoryResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[StoryOut]


class StoryDraftOut(BaseModel):
    id: int
    user_id: int
    media_url: str
    media_type: MediaType
    caption: str | None
    location: LocationOut | None = None
    close_friends_only: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PaginatedStoryDraftsResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[StoryDraftOut]


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


class MediaItemOut(BaseModel):
    """One item of a post's media carousel — see app/models.py PostMedia."""
    id: int
    media_url: str
    media_type: MediaType
    position: int = 0
    caption: str | None = None

    class Config:
        from_attributes = True


class NearbyLocationOut(LocationOut):
    distance_km: float

class NearbyLocationsResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[NearbyLocationOut] = []

class PostDetailOut(PostOut):
    user: UserSummaryOut | None = None
    likes_count: int = 0
    comments_count: int = 0
    share_count: int = 0
    hashtags: list[str] = []
    # All media attached to the post, in upload order. `media_url`/
    # `media_type` above keep mirroring media[0] for any client that only
    # reads the flat fields — this list is the additive, carousel-aware view.
    media: list[MediaItemOut] = []
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


# ---- Hashtags ----

class HashtagOut(BaseModel):
    """GET /api/hashtags/{name} — a hashtag plus how many posts carry it."""
    name: str
    posts_count: int


class TrendingHashtagOut(BaseModel):
    """One row of GET /api/hashtags/trending — a hashtag ranked by recent
    post volume, alongside its all-time post count for context."""
    name: str
    posts_count: int
    recent_posts_count: int


class PaginatedTrendingHashtagsResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[TrendingHashtagOut]


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


class ReelTagOut(BaseModel):
    id: int
    user: UserSummaryOut
    x_position: float | None
    y_position: float | None
    tagged_at: datetime

    class Config:
        from_attributes = True


class ReelTagsResponse(BaseModel):
    message: str
    tags: list[ReelTagOut]


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

class AudioOut(BaseModel):
    id: int
    title: str
    artist: str | None
    audio_url: str
    created_at: datetime

    class Config:
        from_attributes = True


class AudioDetailOut(AudioOut):
    reels_count: int = 0


class PaginatedAudioResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[AudioDetailOut]


class CollaboratorOut(BaseModel):
    id: int
    user: UserSummaryOut
    added_at: datetime

    class Config:
        from_attributes = True


class ReelDetailOut(ReelOut):
    title: str | None = None
    remixed_from_id: int | None = None
    user: UserSummaryOut | None = None
    likes_count: int = 0
    comments_count: int = 0
    is_liked: bool = False
    like_id: int | None = None
    is_saved: bool = False
    location_name: str | None = None
    location_latitude: float | None = None
    location_longitude: float | None = None
    location: LocationOut | None = None
    collaborators: list[CollaboratorOut] = []
    tags_count: int = 0
    tags: list[UserSummaryOut] = []
    audio: AudioOut | None = None


class PaginatedReelDetailResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ReelDetailOut]


class VideoMetadataUpdate(BaseModel):
    """PUT /api/videos/:id/metadata — every field optional so callers can patch just one.
    `location` follows the same shape/semantics as PUT /api/posts/:id's `location`
    field: omit to leave unchanged, send `null` to clear it, or a name(+coordinates)
    object to set/replace it."""
    title: str | None = Field(default=None, max_length=150)
    description: str | None = Field(default=None, max_length=2200, description="Stored as the reel's caption")
    location: LocationIn | None = None

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


class ReelShareLinkResponse(BaseModel):
    reel_id: int
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
    """Text message, or a shared reel. With `shared_reel_id`, `content` is an
    optional note sent alongside the reel; without it, `content` is required."""
    content: str | None = Field(default=None, min_length=1, max_length=2200)
    reply_to_message_id: int | None = Field(
        default=None, gt=0, description="Reply to another message in this conversation"
    )
    shared_reel_id: int | None = Field(
        default=None, gt=0, description="Share this reel into the conversation"
    )

    @field_validator("content")
    @classmethod
    def strip_content(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip() or None

    @model_validator(mode="after")
    def require_content_or_reel(self):
        if self.content is None and self.shared_reel_id is None:
            raise ValueError("Message can't be empty")
        return self


class MessageEditRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=2200)

    @field_validator("content")
    @classmethod
    def strip_content(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Message can't be empty")
        return v


class MessageReactionCreate(BaseModel):
    emoji: str = Field(..., min_length=1, max_length=16)

    @field_validator("emoji")
    @classmethod
    def strip_emoji(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Reaction can't be empty")
        return v


class MessageReactionOut(BaseModel):
    user_id: int
    emoji: str

    class Config:
        from_attributes = True


class SharedReelOut(BaseModel):
    """Preview card for a reel shared in chat, resolved per viewer: if the
    reel was deleted, or the viewer can't see it (private account they don't
    follow, or a block), `is_available` is False and only `reel_id` is set.
    Full detail is GET /api/reels/{reel_id}."""
    reel_id: int
    is_available: bool = True
    video_url: str | None = None
    thumbnail_url: str | None = None
    caption: str | None = None
    duration_seconds: float | None = None
    user: UserSummaryOut | None = None


class MessageOut(BaseModel):
    id: int
    conversation_id: int
    sender_id: int
    content: str | None
    media_url: str | None = None
    media_type: MediaType | None = None
    reply_to_message_id: int | None = None
    reply_to: "MessageRepliedToOut | None" = None
    reply_to_story_id: int | None = None
    # Non-null `shared_reel` means this message is a shared reel; `content`
    # is then just the optional note.
    shared_reel_id: int | None = None
    shared_reel: SharedReelOut | None = None
    is_auto_message: bool = False
    edited_at: datetime | None = None
    is_deleted: bool = False
    reactions: list[MessageReactionOut] = []
    # "sent" | "delivered" | "read" — see models.MessageStatus. Always
    # "sent" from the sender's own point of view isn't tracked separately;
    # this reflects the furthest state any recipient has reached.
    status: str = "sent"
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
    # This viewer's own participant status — "pending" means the thread is
    # sitting in their Message Requests until they accept/decline (see
    # chat_routes.get_message_requests / accept_conversation).
    status: str = "accepted"
    is_muted: bool = False
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


# ==========================================================================
# Creator earnings ledger
# ==========================================================================

class CreatorEarningOut(BaseModel):
    id: int
    source_type: EarningSourceType
    source_id: int | None
    amount_cents: int
    currency: str
    description: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class PaginatedCreatorEarningsResponse(BaseModel):
    total: int
    limit: int
    offset: int
    # All-time total across every ledger row (not just this page).
    total_earnings_cents: int
    currency: str
    items: list[CreatorEarningOut]


class CreatorEarningsSummaryOut(BaseModel):
    total_earnings_cents: int
    currency: str
    by_source: dict[str, int]
    monetization: MonetizationStatusOut


# ==========================================================================
# Creator-to-creator collaboration requests
# ==========================================================================

class CreatorCollaborationCreate(BaseModel):
    partner_user_id: int = Field(..., gt=0)
    # Optional: propose collaborating on a reel you already own. If set, the
    # reel must belong to the requester.
    reel_id: int | None = Field(default=None, gt=0)
    message: str | None = Field(default=None, max_length=500)
    proposed_revenue_share_percentage: int | None = Field(default=None, ge=0, le=100)
    proposed_amount_cents: int | None = Field(default=None, ge=0)

    @field_validator("message")
    @classmethod
    def strip_message(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else v


class CreatorCollaborationOut(BaseModel):
    id: int
    requester: UserSummaryOut
    partner: UserSummaryOut
    reel_id: int | None
    message: str | None
    proposed_revenue_share_percentage: int | None
    proposed_amount_cents: int | None
    status: CollaborationStatus
    created_at: datetime
    responded_at: datetime | None

    class Config:
        from_attributes = True


class PaginatedCreatorCollaborationResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[CreatorCollaborationOut]


# ==========================================================================
# Brand (paid-partnership) collaborations
# ==========================================================================

class BrandCollaborationCreate(BaseModel):
    creator_user_id: int = Field(..., gt=0)
    brand_name: str = Field(..., min_length=1, max_length=150)
    brand_contact_email: str | None = Field(default=None, max_length=255)
    campaign_title: str = Field(..., min_length=1, max_length=150)
    campaign_description: str | None = Field(default=None, max_length=1000)
    offer_amount_cents: int = Field(..., ge=0)
    currency: str = Field(default="INR", max_length=10)
    deliverables: str | None = Field(default=None, max_length=1000)

    @field_validator("brand_contact_email")
    @classmethod
    def validate_email(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not _EMAIL_RE.match(v):
            raise ValueError("Invalid email address")
        return v

    @field_validator("brand_name", "campaign_title", "campaign_description", "deliverables")
    @classmethod
    def strip_text_fields(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else v


class BrandCollaborationOut(BaseModel):
    id: int
    created_by: UserSummaryOut
    creator: UserSummaryOut
    brand_name: str
    brand_contact_email: str | None
    campaign_title: str
    campaign_description: str | None
    offer_amount_cents: int
    currency: str
    deliverables: str | None
    status: BrandCollaborationStatus
    created_at: datetime
    responded_at: datetime | None

    class Config:
        from_attributes = True


class PaginatedBrandCollaborationResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[BrandCollaborationOut]


# ==========================================================================
# Unified search
# ==========================================================================

class SearchUserOut(BaseModel):
    id: int
    username: str
    full_name: str | None
    avatar_url: str | None
    is_private: bool
    account_type: AccountType
    is_following: bool = False

    class Config:
        from_attributes = True


class SearchSongOut(BaseModel):
    id: int
    title: str
    artist: str | None
    audio_url: str

    class Config:
        from_attributes = True


class SearchAllResult(BaseModel):
    users: list[SearchUserOut]
    songs: list[SearchSongOut]
    locations: list[LocationOut]


class PaginatedSearchUsersResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[SearchUserOut]


class PaginatedSearchSongsResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[SearchSongOut]


# ---- Privacy: block / restrict / mute ----

class BlockActionResponse(BaseModel):
    message: str
    is_blocked: bool


class RestrictActionResponse(BaseModel):
    message: str
    is_restricted: bool


class CloseFriendActionResponse(BaseModel):
    message: str
    is_close_friend: bool


class MuteRequest(BaseModel):
    """POST /api/privacy/mute/:user_id — at least one of the two must be
    true (both default true, matching tapping "Mute" in the app, which
    mutes both by default)."""
    mute_posts: bool = True
    mute_stories: bool = True


class MuteOut(BaseModel):
    user: UserSummaryOut
    mute_posts: bool
    mute_stories: bool


class MuteActionResponse(BaseModel):
    message: str
    mute_posts: bool
    mute_stories: bool


class PaginatedMutedUsersResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[MuteOut]


# ---- Advanced stories: mentions / polls / questions ----

class StoryMentionOut(BaseModel):
    id: int
    user: UserSummaryOut
    created_at: datetime

    class Config:
        from_attributes = True


class StoryPollOptionIn(BaseModel):
    text: str = Field(..., max_length=60)


class StoryPollIn(BaseModel):
    """Attach a poll sticker at story-creation time. Instagram's own poll
    sticker is always exactly two options, so that's what's enforced here
    (min_length=max_length=2) — see models.StoryPoll's docstring for why
    the table itself doesn't hardcode that."""
    question: str = Field(..., max_length=150)
    options: list[StoryPollOptionIn] = Field(..., min_length=2, max_length=2)


class StoryPollOptionOut(BaseModel):
    id: int
    text: str
    votes_count: int = 0

    class Config:
        from_attributes = True


class StoryPollOut(BaseModel):
    id: int
    question: str
    options: list[StoryPollOptionOut]
    total_votes: int = 0
    my_vote_option_id: int | None = None

    class Config:
        from_attributes = True


class StoryPollVoteRequest(BaseModel):
    option_id: int = Field(..., gt=0)


class StoryQuestionIn(BaseModel):
    """Attach a question sticker ("Ask me anything") at story-creation time."""
    prompt: str = Field(..., max_length=150)


class StoryQuestionResponseIn(BaseModel):
    response_text: str = Field(..., min_length=1, max_length=500)

    @field_validator("response_text")
    @classmethod
    def strip_response(cls, v: str) -> str:
        return v.strip()


class StoryQuestionResponseOut(BaseModel):
    id: int
    user: UserSummaryOut
    response_text: str
    created_at: datetime

    class Config:
        from_attributes = True


class StoryQuestionOut(BaseModel):
    id: int
    prompt: str
    responses_count: int = 0

    class Config:
        from_attributes = True


class StoryQuestionResponsesResponse(BaseModel):
    prompt: str
    items: list[StoryQuestionResponseOut]


# ---- Advanced DM: media messages, replies, message requests ----

class MessageRepliedToOut(BaseModel):
    """Compact preview of the message being replied to — enough to render
    a reply-quote UI without a second fetch."""
    id: int
    sender_id: int
    content: str | None
    media_type: MediaType | None
    is_deleted: bool
    is_reel_share: bool = False

    class Config:
        from_attributes = True


class MediaMessageResponse(BaseModel):
    """Returned by POST /api/chat/conversations/:id/media — same shape as
    MessageOut, kept as its own name since it's documented as a distinct
    upload flow in the API reference; the schema itself doesn't diverge."""
    id: int
    conversation_id: int
    sender_id: int
    content: str | None
    media_url: str | None
    media_type: MediaType | None
    reply_to_message_id: int | None
    created_at: datetime

    class Config:
        from_attributes = True


class ConversationRequestActionResponse(BaseModel):
    message: str
    conversation_id: int


# ==========================================================================
# Live
# ==========================================================================

class LiveCreate(BaseModel):
    title: str | None = Field(default=None, max_length=150)


class LiveSessionOut(BaseModel):
    id: int
    user: UserSummaryOut
    title: str | None
    status: LiveStatus
    started_at: datetime
    ended_at: datetime | None
    viewer_count: int = 0
    likes_count: int = 0

    class Config:
        from_attributes = True


class PaginatedLiveSessionsResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[LiveSessionOut]


class LiveActionResponse(BaseModel):
    message: str
    live: LiveSessionOut


class LiveViewerOut(BaseModel):
    id: int
    user: UserSummaryOut
    joined_at: datetime
    left_at: datetime | None

    class Config:
        from_attributes = True


class PaginatedLiveViewersResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[LiveViewerOut]


class LiveViewerCountResponse(BaseModel):
    live_id: int
    viewer_count: int


class LiveCommentCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=500)


class LiveCommentOut(BaseModel):
    id: int
    user: UserSummaryOut
    content: str
    created_at: datetime

    class Config:
        from_attributes = True


class PaginatedLiveCommentsResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[LiveCommentOut]


class LiveLikeActionResponse(BaseModel):
    message: str
    liked: bool
    likes_count: int


# ==========================================================================
# Audio management (create/save) — audio discovery (details/reels/trending)
# already has its own schemas in the Reels section above.
# ==========================================================================

class SavedAudioActionResponse(BaseModel):
    message: str
    is_saved: bool


class PaginatedSavedAudioResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[AudioOut]


# ==========================================================================
# Reports / Moderation
# ==========================================================================

class ReportCreate(BaseModel):
    target_type: ReportTargetType
    target_id: int = Field(..., gt=0)
    reason: ReportReason
    description: str | None = Field(default=None, max_length=1000)


class ReportOut(BaseModel):
    id: int
    target_type: ReportTargetType
    target_id: int
    reason: ReportReason
    description: str | None
    status: ReportStatus
    action: ReportAction | None
    created_at: datetime
    reviewed_at: datetime | None

    class Config:
        from_attributes = True


class PaginatedReportsResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ReportOut]


class AdminReportOut(ReportOut):
    reporter: UserSummaryOut
    reviewed_by: UserSummaryOut | None = None


class PaginatedAdminReportsResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[AdminReportOut]


class ReportStatusUpdate(BaseModel):
    status: ReportStatus
    action: ReportAction | None = Field(
        default=None,
        description="Optional side-effecting action to take alongside the status change "
        "— see models.ReportAction for what each one does.",
    )


# ==========================================================================
# Hashtag follow
# ==========================================================================

class HashtagFollowActionResponse(BaseModel):
    message: str
    is_following: bool


class PaginatedHashtagsResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[HashtagOut]


# ==========================================================================
# Recent search
# ==========================================================================

class RecentSearchCreate(BaseModel):
    query_text: str | None = Field(default=None, max_length=150)
    target_user_id: int | None = None

    @model_validator(mode="after")
    def _exactly_one(self):
        if bool(self.query_text) == bool(self.target_user_id):
            raise ValueError("Provide exactly one of query_text or target_user_id")
        return self


class RecentSearchOut(BaseModel):
    id: int
    query_text: str | None
    target_user: UserSummaryOut | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class PaginatedRecentSearchResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[RecentSearchOut]


# ==========================================================================
# Notification preferences
# ==========================================================================

class NotificationPreferencesOut(BaseModel):
    likes_enabled: bool
    comments_enabled: bool
    follows_enabled: bool
    mentions_enabled: bool
    messages_enabled: bool
    collaboration_requests_enabled: bool
    push_enabled: bool

    class Config:
        from_attributes = True


class NotificationPreferencesUpdate(BaseModel):
    likes_enabled: bool | None = None
    comments_enabled: bool | None = None
    follows_enabled: bool | None = None
    mentions_enabled: bool | None = None
    messages_enabled: bool | None = None
    collaboration_requests_enabled: bool | None = None
    push_enabled: bool | None = None


# ==========================================================================
# Account / security
# ==========================================================================

class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: str) -> str:
        return _validate_password_strength(v)


# ==========================================================================
# Follower management
# ==========================================================================

class FollowStatusOut(BaseModel):
    is_following: bool
    is_followed_by: bool
    request_pending: bool
