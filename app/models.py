import enum

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship

from app.database import Base


class OTPChannel(str, enum.Enum):
    phone = "phone"
    email = "email"


class OTPPurpose(str, enum.Enum):
    signup = "signup"
    login = "login"
    reset_password = "reset_password"


class Gender(str, enum.Enum):
    male = "male"
    female = "female"
    non_binary = "non_binary"
    prefer_not_to_say = "prefer_not_to_say"


class MediaType(str, enum.Enum):
    image = "image"
    video = "video"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    # Chosen during signup, must be globally unique — checked ahead of time via
    # /check-username, same pattern as Instagram's signup flow.
    username = Column(String(30), unique=True, index=True, nullable=False)
    phone_number = Column(String(20), unique=True, index=True, nullable=True)
    email = Column(String(255), unique=True, index=True, nullable=True)
    # Nullable: users who only ever sign in via phone/email OTP have no password set.
    hashed_password = Column(String(255), nullable=True)
    date_of_birth = Column(Date, nullable=True)
    gender = Column(Enum(Gender), nullable=True)
    # Profile fields (displayed on the profile page, editable via PUT /api/users/:id)
    full_name = Column(String(100), nullable=True)
    bio = Column(String(150), nullable=True)
    avatar_url = Column(String(500), nullable=True)
    is_private = Column(Boolean, default=False, nullable=False)
    is_phone_verified = Column(Boolean, default=False, nullable=False)
    is_email_verified = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    refresh_tokens = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan"
    )
    posts = relationship("Post", back_populates="user", cascade="all, delete-orphan")
    reels = relationship("Reel", back_populates="user", cascade="all, delete-orphan")
    stories = relationship("Story", back_populates="user", cascade="all, delete-orphan")


class PendingSignup(Base):
    """
    Holds a signup's username/password/DOB after /register but before the OTP
    step confirms it. The real User row is only created once /verify-otp
    succeeds for purpose=signup — this keeps unverified identifiers/usernames
    from permanently reserving a spot, matching Instagram's "you're not
    signed up until you enter the code" behavior.
    """

    __tablename__ = "pending_signups"

    id = Column(Integer, primary_key=True, index=True)
    # One pending signup per identifier; a repeat /register call overwrites it.
    identifier = Column(String(255), unique=True, index=True, nullable=False)
    channel = Column(Enum(OTPChannel), nullable=False)
    username = Column(String(30), nullable=False)
    hashed_password = Column(String(255), nullable=False)
    date_of_birth = Column(Date, nullable=False)
    gender = Column(Enum(Gender), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class OTP(Base):
    __tablename__ = "otp_codes"

    id = Column(Integer, primary_key=True, index=True)
    # Phone number (E.164) or email address, depending on `channel`.
    identifier = Column(String(255), index=True, nullable=False)
    channel = Column(Enum(OTPChannel), nullable=False)
    purpose = Column(Enum(OTPPurpose), nullable=False)
    otp_hash = Column(String(255), nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    is_used = Column(Boolean, default=False, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Only a hash of the refresh token is stored — the raw value is returned to
    # the client once and never persisted, same pattern as the OTP hashing.
    token_hash = Column(String(255), unique=True, index=True, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="refresh_tokens")


class Follow(Base):
    """
    A directed edge: `follower_id` follows `following_id`. No approval step
    for private accounts is modeled here — follows are effective immediately.
    """

    __tablename__ = "follows"
    __table_args__ = (
        UniqueConstraint("follower_id", "following_id", name="uq_follow_pair"),
    )

    id = Column(Integer, primary_key=True, index=True)
    follower_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    following_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    follower = relationship("User", foreign_keys=[follower_id])
    following = relationship("User", foreign_keys=[following_id])


class Post(Base):
    __tablename__ = "posts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    caption = Column(Text, nullable=True)
    media_url = Column(String(500), nullable=False)
    media_type = Column(Enum(MediaType), default=MediaType.image, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="posts")
    saves = relationship("SavedPost", back_populates="post", cascade="all, delete-orphan")


class Reel(Base):
    __tablename__ = "reels"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    caption = Column(Text, nullable=True)
    video_url = Column(String(500), nullable=False)
    thumbnail_url = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="reels")


class WatchSession(Base):
    """
    One row per start/end cycle of a user watching a single reel — this is
    what makes "actual watch time" possible instead of just app-open time.

    `active_owner_id` mirrors `user_id` while the session is open (started
    but not yet ended) and is set back to NULL the moment it ends or is
    auto-closed by a later /watch/start. MySQL allows multiple NULLs through
    a unique index, so the UniqueConstraint below enforces "at most one open
    session per user" at the database level — the same trick used for
    partial unique indexes in Postgres, adapted for MySQL. This closes the
    race window that an application-level check alone would leave open
    between two near-simultaneous /watch/start calls.
    """

    __tablename__ = "watch_sessions"
    __table_args__ = (
        UniqueConstraint("active_owner_id", name="uq_watch_session_active_owner"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    reel_id = Column(Integer, ForeignKey("reels.id"), nullable=False, index=True)

    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    watch_seconds = Column(Integer, nullable=True)

    # NULL once ended; see class docstring for why this exists.
    active_owner_id = Column(Integer, nullable=True, index=True)

    # Sessions under the "ignore short sessions" floor are kept for audit
    # (bot/abuse analysis) but excluded from history + stats via this flag
    # rather than being deleted outright.
    is_valid = Column(Boolean, default=True, nullable=False, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", foreign_keys=[user_id])
    reel = relationship("Reel", foreign_keys=[reel_id])


class SavedPost(Base):
    """A user bookmarking a post — powers GET /api/users/:id/saved."""

    __tablename__ = "saved_posts"
    __table_args__ = (
        UniqueConstraint("user_id", "post_id", name="uq_saved_post"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", foreign_keys=[user_id])
    post = relationship("Post", back_populates="saves")


class Story(Base):
    """
    Stories are ephemeral (default 24h). `expires_at` is set at creation time
    and every read query filters on it, so an expired story disappears from
    the API immediately even before the cleanup job removes the row.

    Actual deletion of expired rows is handled out-of-band by a scheduled job
    (RDS/cron) that runs `app/cleanup_expired_stories.py` — see that file.
    """

    __tablename__ = "stories"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    media_url = Column(String(500), nullable=False)
    media_type = Column(Enum(MediaType), default=MediaType.image, nullable=False)
    caption = Column(String(280), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)

    user = relationship("User", back_populates="stories")
    views = relationship("StoryView", back_populates="story", cascade="all, delete-orphan")


class StoryView(Base):
    """Records that `viewer_id` has seen `story_id` — powers the viewers list / view count."""

    __tablename__ = "story_views"
    __table_args__ = (
        UniqueConstraint("story_id", "viewer_id", name="uq_story_view"),
    )

    id = Column(Integer, primary_key=True, index=True)
    story_id = Column(Integer, ForeignKey("stories.id"), nullable=False, index=True)
    viewer_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    viewed_at = Column(DateTime(timezone=True), server_default=func.now())

    story = relationship("Story", back_populates="views")
    viewer = relationship("User", foreign_keys=[viewer_id])
