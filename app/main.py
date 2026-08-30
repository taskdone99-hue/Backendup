import enum

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
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


class LikeTargetType(str, enum.Enum):
    post = "post"
    reel = "reel"
    comment = "comment"


class ShareContentType(str, enum.Enum):
    post = "post"
    reel = "reel"


class SavedItemType(str, enum.Enum):
    """What a bookmark points at — powers the Saved tab's category filter
    (All / Posts / Reels / Audio / Series)."""
    post = "post"
    reel = "reel"
    audio = "audio"
    series = "series"


class NotificationType(str, enum.Enum):
    like = "like"
    comment = "comment"
    follow = "follow"
    follow_request = "follow_request"
    mention = "mention"
    share = "share"
    message = "message"
    other = "other"


class MembershipInterval(str, enum.Enum):
    monthly = "monthly"
    yearly = "yearly"


class MembershipStatus(str, enum.Enum):
    active = "active"
    canceled = "canceled"
    expired = "expired"
    past_due = "past_due"


class PaymentProvider(str, enum.Enum):
    razorpay = "razorpay"
    stripe = "stripe"


class PaymentStatus(str, enum.Enum):
    created = "created"
    paid = "paid"
    failed = "failed"
    refunded = "refunded"


class DevicePlatform(str, enum.Enum):
    ios = "ios"
    android = "android"
    web = "web"


class AccountType(str, enum.Enum):
    """Personal vs. business profile — drives the auto-intro DM sent the
    first time someone messages this user (see chat_routes.create_conversation)."""
    personal = "personal"
    business = "business"


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
    # Business-profile fields — only meaningful when account_type is
    # business. business_name/business_category surface on the profile;
    # business_description feeds the auto-intro DM's body text.
    account_type = Column(Enum(AccountType), default=AccountType.personal, nullable=False)
    business_name = Column(String(100), nullable=True)
    business_category = Column(String(100), nullable=True)
    business_description = Column(String(500), nullable=True)
    # Per-user chat display-font preference — PUT /api/chat/settings/font.
    # Nullable/free-form on purpose: the client owns the list of valid font
    # names, same way it owns theme names, so the API doesn't hardcode one.
    chat_font = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    refresh_tokens = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan"
    )
    posts = relationship("Post", back_populates="user", cascade="all, delete-orphan")
    reels = relationship("Reel", back_populates="user", cascade="all, delete-orphan")
    stories = relationship("Story", back_populates="user", cascade="all, delete-orphan")
    highlights = relationship(
        "Highlight", back_populates="user", cascade="all, delete-orphan"
    )
    snaps = relationship("Snap", back_populates="user", cascade="all, delete-orphan")
    device_tokens = relationship(
        "DeviceToken", back_populates="user", cascade="all, delete-orphan"
    )


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
    A directed edge: `follower_id` follows `following_id`. For a private
    account, this row only gets created once the target accepts the
    corresponding FollowRequest below — a public account still goes
    straight to a Follow row with no approval step, same as before.
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


class FollowRequest(Base):
    """
    A pending follow request to a private account. Only pending requests
    are stored here — accepting one deletes the row and creates the real
    Follow row instead; rejecting just deletes the row. There's
    deliberately no `status` column: a row existing at all means pending.
    """

    __tablename__ = "follow_requests"
    __table_args__ = (
        UniqueConstraint("requester_id", "target_id", name="uq_follow_request_pair"),
    )

    id = Column(Integer, primary_key=True, index=True)
    requester_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    target_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    requester = relationship("User", foreign_keys=[requester_id])
    target = relationship("User", foreign_keys=[target_id])


class Post(Base):
    __tablename__ = "posts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    caption = Column(Text, nullable=True)
    media_url = Column(String(500), nullable=False)
    media_type = Column(Enum(MediaType), default=MediaType.image, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Post-detail extras — Add Music / Add Location. Both are optional and
    # inherently one-per-post, so plain nullable columns (not a separate
    # table) keep a post fetch to a single row read.
    music_title = Column(String(150), nullable=True)
    music_artist = Column(String(150), nullable=True)
    music_url = Column(String(500), nullable=True)
    music_start_seconds = Column(Integer, nullable=True)

    location_name = Column(String(150), nullable=True)
    location_latitude = Column(Float, nullable=True)
    location_longitude = Column(Float, nullable=True)

    # Accessibility text read out by screen readers — Instagram's "Alt text".
    alt_text = Column(String(1000), nullable=True)
    # "AI info" disclosure label — whether the post's media was created or
    # edited with AI (Instagram's "AI-generated content" toggle).
    ai_generated = Column(Boolean, default=False, nullable=False)

    user = relationship("User", back_populates="posts")
    saves = relationship("SavedPost", back_populates="post", cascade="all, delete-orphan")
    tag_rows = relationship("PostTag", back_populates="post", cascade="all, delete-orphan")
    member_rows = relationship("PostMember", back_populates="post", cascade="all, delete-orphan")


class PostTag(Base):
    """A user tagged in a post's media — powers POST /api/posts/:id/tags.
    x_position/y_position are optional normalized (0.0-1.0) coordinates for
    placing the tag bubble on the image, same idea as Instagram's tap-to-tag."""

    __tablename__ = "post_tags"
    __table_args__ = (
        UniqueConstraint("post_id", "user_id", name="uq_post_tag"),
    )

    id = Column(Integer, primary_key=True, index=True)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    x_position = Column(Float, nullable=True)
    y_position = Column(Float, nullable=True)
    tagged_at = Column(DateTime(timezone=True), server_default=func.now())

    post = relationship("Post", back_populates="tag_rows")
    user = relationship("User", foreign_keys=[user_id])


class PostMember(Base):
    """A user added as a member/co-author on a post — powers
    POST /api/posts/:id/members. Same pattern as ReelCollaborator below,
    just for posts."""

    __tablename__ = "post_members"
    __table_args__ = (
        UniqueConstraint("post_id", "user_id", name="uq_post_member"),
    )

    id = Column(Integer, primary_key=True, index=True)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    added_at = Column(DateTime(timezone=True), server_default=func.now())

    post = relationship("Post", back_populates="member_rows")
    user = relationship("User", foreign_keys=[user_id])


class Reel(Base):
    __tablename__ = "reels"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    caption = Column(Text, nullable=True)
    # Separate from `caption` — metadata endpoints (PUT /api/videos/:id/metadata)
    # treat title/description as distinct fields; caption doubles as description.
    title = Column(String(150), nullable=True)
    video_url = Column(String(500), nullable=False)
    thumbnail_url = Column(String(500), nullable=True)
    # Deliberately NOT a ForeignKey: a real FK with the default RESTRICT
    # behavior would block deleting an original reel once anything remixes
    # its audio. This is an informal reference to reels.id instead — a
    # remix that outlives its original just keeps a dangling id.
    remixed_from_id = Column(Integer, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="reels")
    collaborators = relationship(
        "ReelCollaborator", back_populates="reel", cascade="all, delete-orphan"
    )
    revenue_splits = relationship(
        "ReelRevenueShare", back_populates="reel", cascade="all, delete-orphan"
    )


class ReelCollaborator(Base):
    """A user tagged as a co-creator on a reel — powers POST /api/videos/:id/collaborators."""

    __tablename__ = "reel_collaborators"
    __table_args__ = (
        UniqueConstraint("reel_id", "user_id", name="uq_reel_collaborator"),
    )

    id = Column(Integer, primary_key=True, index=True)
    reel_id = Column(Integer, ForeignKey("reels.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    added_at = Column(DateTime(timezone=True), server_default=func.now())

    reel = relationship("Reel", back_populates="collaborators")
    user = relationship("User", foreign_keys=[user_id])


class ReelRevenueShare(Base):
    """
    One row per (reel, user) revenue share. All rows for a given reel are
    validated (in the schema layer) to sum to 100 before being written —
    PUT /api/videos/:id/revenue-split replaces the full set atomically.
    """

    __tablename__ = "reel_revenue_shares"
    __table_args__ = (
        UniqueConstraint("reel_id", "user_id", name="uq_reel_revenue_share"),
    )

    id = Column(Integer, primary_key=True, index=True)
    reel_id = Column(Integer, ForeignKey("reels.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    percentage = Column(Integer, nullable=False)  # whole percent, 0-100

    reel = relationship("Reel", back_populates="revenue_splits")
    user = relationship("User", foreign_keys=[user_id])


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
    """
    Legacy post-only bookmark table. Superseded by SavedItem below, which
    covers posts/reels/audio/series in one place — kept only so
    old rows aren't lost; see add_saved_items_tables.py for the one-time
    copy into saved_items. Nothing writes new rows here anymore.
    """

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


class Series(Base):
    """
    A creator-authored ordered set of reels ("episodes") — e.g. a recipe
    series or a multi-part story — separate from bookmarking. Users can
    then save the whole series via SavedItem(target_type=series).

    ASSUMPTION: "Series" wasn't an existing concept anywhere in this
    codebase, so this is a best guess at what it means (a reel playlist
    authored by its creator). Flag if the product intent is different.
    """

    __tablename__ = "series"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(150), nullable=False)
    cover_url = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", foreign_keys=[user_id])
    reels = relationship(
        "SeriesReel", back_populates="series", cascade="all, delete-orphan",
        order_by="SeriesReel.position",
    )


class SeriesReel(Base):
    """One reel's position within a Series (its "episode" order)."""

    __tablename__ = "series_reels"
    __table_args__ = (
        UniqueConstraint("series_id", "reel_id", name="uq_series_reel"),
    )

    id = Column(Integer, primary_key=True, index=True)
    series_id = Column(Integer, ForeignKey("series.id"), nullable=False, index=True)
    reel_id = Column(Integer, ForeignKey("reels.id"), nullable=False, index=True)
    position = Column(Integer, nullable=False, default=0)

    series = relationship("Series", back_populates="reels")
    reel = relationship("Reel", foreign_keys=[reel_id])


class Audio(Base):
    """
    A saveable "sound" — title/artist/audio_url, optionally traced back to
    the post or reel it was first used on. Posts/reels don't reference this
    table for their own playback (they keep their own music_* columns /
    audio, same as today) — this table only exists so a sound can be
    bookmarked and reused independently of the post it came from, same as
    Instagram's "Save Audio".

    ASSUMPTION: there was no Audio/Sound entity anywhere in this codebase
    before now — flag if "Audio" in the Saved tab was meant to mean
    something else.
    """

    __tablename__ = "audio_tracks"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(150), nullable=False)
    artist = Column(String(150), nullable=True)
    audio_url = Column(String(500), nullable=False, unique=True)
    source_post_id = Column(Integer, ForeignKey("posts.id"), nullable=True)
    source_reel_id = Column(Integer, ForeignKey("reels.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SavedCollection(Base):
    """A user-created named folder for organizing saved items (Instagram
    'Collections'). A saved item can live in any number of collections."""

    __tablename__ = "saved_collections"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    cover_url = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", foreign_keys=[user_id])
    items = relationship(
        "SavedCollectionItem", back_populates="collection", cascade="all, delete-orphan"
    )


class SavedItem(Base):
    """
    Unified bookmark row covering posts, reels, audio, and series in one
    table via target_type/target_id — same discriminator pattern as
    LikeTargetType/ShareContentType above. Replaces SavedPost.
    """

    __tablename__ = "saved_items"
    __table_args__ = (
        UniqueConstraint("user_id", "target_type", "target_id", name="uq_saved_item"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    target_type = Column(Enum(SavedItemType), nullable=False)
    target_id = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", foreign_keys=[user_id])
    collections = relationship(
        "SavedCollectionItem", back_populates="saved_item", cascade="all, delete-orphan"
    )


class SavedCollectionItem(Base):
    """One saved item's membership in one collection folder."""

    __tablename__ = "saved_collection_items"
    __table_args__ = (
        UniqueConstraint("collection_id", "saved_item_id", name="uq_saved_collection_item"),
    )

    id = Column(Integer, primary_key=True, index=True)
    collection_id = Column(Integer, ForeignKey("saved_collections.id"), nullable=False, index=True)
    saved_item_id = Column(Integer, ForeignKey("saved_items.id"), nullable=False, index=True)
    added_at = Column(DateTime(timezone=True), server_default=func.now())

    collection = relationship("SavedCollection", back_populates="items")
    saved_item = relationship("SavedItem", back_populates="collections")


class Comment(Base):
    """
    Comments live on posts or reels — exactly one of post_id/reel_id is set
    per row. `parent_id` set means this row is a reply — replies are one
    level deep, matching how POST /api/comments/:id/reply and
    GET /api/posts/:postId/comments / GET /api/reels/:reelId/comments
    (which only return top-level comments) are implemented.
    """

    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, index=True)
    # Exactly one of post_id / reel_id is set.
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=True, index=True)
    reel_id = Column(Integer, ForeignKey("reels.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    parent_id = Column(Integer, ForeignKey("comments.id"), nullable=True, index=True)
    content = Column(String(2200), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    post = relationship("Post", foreign_keys=[post_id])
    user = relationship("User", foreign_keys=[user_id])
    parent = relationship("Comment", remote_side=[id], foreign_keys=[parent_id])


class Like(Base):
    """
    A single generic like table for posts, reels, and comments — `target_type`
    + `target_id` say what was liked instead of three separate like tables.
    `target_id` is intentionally not a ForeignKey since it points at three
    different tables depending on `target_type`; the routers validate the
    target exists before inserting.
    """

    __tablename__ = "likes"
    __table_args__ = (
        UniqueConstraint("user_id", "target_type", "target_id", name="uq_like_target"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    target_type = Column(Enum(LikeTargetType), nullable=False, index=True)
    target_id = Column(Integer, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", foreign_keys=[user_id])


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
    reactions = relationship("StoryReaction", back_populates="story", cascade="all, delete-orphan")


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


class StoryReaction(Base):
    """
    A single emoji reaction from `user_id` on `story_id` — one per (story, user),
    tapping a new emoji just replaces the previous one, same as Instagram.
    """

    __tablename__ = "story_reactions"
    __table_args__ = (
        UniqueConstraint("story_id", "user_id", name="uq_story_reaction"),
    )

    id = Column(Integer, primary_key=True, index=True)
    story_id = Column(Integer, ForeignKey("stories.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    emoji = Column(String(16), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    story = relationship("Story", back_populates="reactions")
    user = relationship("User", foreign_keys=[user_id])


# ==========================================================================
# Story Highlights
# ==========================================================================

class Highlight(Base):
    """A named, non-expiring collection of story snapshots pinned to a profile."""

    __tablename__ = "highlights"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(50), nullable=False)
    cover_url = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="highlights")
    items = relationship(
        "HighlightItem",
        back_populates="highlight",
        cascade="all, delete-orphan",
        order_by="HighlightItem.added_at",
    )


class HighlightItem(Base):
    """
    A snapshot of a story's media copied into a highlight at add-time. Copying
    (rather than pointing at the Story row) is deliberate: stories expire and
    get cleaned up after STORY_LIFETIME_HOURS, but a highlight is meant to
    outlive that — so it keeps its own independent copy of the media.
    """

    __tablename__ = "highlight_items"

    id = Column(Integer, primary_key=True, index=True)
    highlight_id = Column(Integer, ForeignKey("highlights.id"), nullable=False, index=True)
    # Informal reference only (like Reel.remixed_from_id) — the source story
    # may have expired and been deleted by the time this is read.
    source_story_id = Column(Integer, nullable=True, index=True)
    media_url = Column(String(500), nullable=False)
    media_type = Column(Enum(MediaType), default=MediaType.image, nullable=False)
    caption = Column(String(280), nullable=True)
    added_at = Column(DateTime(timezone=True), server_default=func.now())

    highlight = relationship("Highlight", back_populates="items")


# ==========================================================================
# Share
# ==========================================================================

class Share(Base):
    """
    A record of a post/reel being sent to another user in-app, powering
    POST /api/share/internal. `content_id` is intentionally not a ForeignKey
    since it points at either posts or reels depending on `content_type`,
    same pattern as Like.target_id.
    """

    __tablename__ = "shares"

    id = Column(Integer, primary_key=True, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    recipient_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    content_type = Column(Enum(ShareContentType), nullable=False)
    content_id = Column(Integer, nullable=False, index=True)
    message = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    sender = relationship("User", foreign_keys=[sender_id])
    recipient = relationship("User", foreign_keys=[recipient_id])


# ==========================================================================
# Snap / Camera & Filters
# ==========================================================================

class Snap(Base):
    """
    A filter-tagged camera capture, powering POST /api/snaps. The catalog of
    AR filters (GET /api/filters) is a small static list served straight from
    the router rather than a DB table, so `filter_id` is stored as a plain
    string id from that catalog instead of a ForeignKey.
    """

    __tablename__ = "snaps"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    media_url = Column(String(500), nullable=False)
    media_type = Column(Enum(MediaType), default=MediaType.image, nullable=False)
    filter_id = Column(String(50), nullable=True)
    caption = Column(String(280), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="snaps")


# ==========================================================================
# Chat
# ==========================================================================

class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    is_group = Column(Boolean, default=False, nullable=False)
    # Only meaningful for group conversations; 1:1 chats derive their display
    # name client-side from the other participant.
    title = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    participants = relationship(
        "ConversationParticipant", back_populates="conversation", cascade="all, delete-orphan"
    )
    messages = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )


class ConversationParticipant(Base):
    __tablename__ = "conversation_participants"
    __table_args__ = (
        UniqueConstraint("conversation_id", "user_id", name="uq_conversation_participant"),
    )

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    joined_at = Column(DateTime(timezone=True), server_default=func.now())
    # Read-receipt watermark: the highest Message.id this participant has
    # seen in this conversation. NULL means nothing read yet.
    last_read_message_id = Column(Integer, nullable=True)

    conversation = relationship("Conversation", back_populates="participants")
    user = relationship("User", foreign_keys=[user_id])


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    content = Column(String(2200), nullable=False)
    # Set when this message is a "story reply" (tapping reply on someone's
    # story sends a DM). SET NULL on delete so replying to a story that later
    # expires/gets removed doesn't wipe out the DM history.
    reply_to_story_id = Column(
        Integer, ForeignKey("stories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # True for the one-time auto-intro DM sent on a brand-new 1:1
    # conversation (see chat_routes.create_conversation) — lets a client
    # style/skip it differently from a message the sender actually typed.
    is_auto_message = Column(Boolean, default=False, nullable=False)
    edited_at = Column(DateTime(timezone=True), nullable=True)
    # Soft delete — the row (and its history) stays for moderation/audit,
    # but the API never returns `content` once this is set; see
    # chat_routes._to_message_out. Keeps other participants' message
    # ordering/reply context intact instead of leaving a hole.
    is_deleted = Column(Boolean, default=False, nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    conversation = relationship("Conversation", back_populates="messages")
    sender = relationship("User", foreign_keys=[sender_id])
    reactions = relationship(
        "MessageReaction", back_populates="message", cascade="all, delete-orphan"
    )
    statuses = relationship(
        "MessageStatus", back_populates="message", cascade="all, delete-orphan"
    )


class MessageReaction(Base):
    """A single emoji reaction from `user_id` on `message_id` — one per
    (message, user); re-tapping replaces the emoji, same convention as
    StoryReaction."""

    __tablename__ = "message_reactions"
    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="uq_message_reaction"),
    )

    id = Column(Integer, primary_key=True, index=True)
    message_id = Column(Integer, ForeignKey("messages.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    emoji = Column(String(16), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    message = relationship("Message", back_populates="reactions")
    user = relationship("User", foreign_keys=[user_id])


class MessageStatus(Base):
    """
    Per-recipient delivery/read tracking for one message — one row per
    (message, recipient). Powers the sent/delivered/read indicator:
    a message with no rows yet (or none delivered) is "sent"; once every
    recipient's row has delivered_at it's "delivered"; once every
    recipient's row has read_at it's "read". The sender doesn't get a row
    for their own message.
    """

    __tablename__ = "message_statuses"
    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="uq_message_status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    message_id = Column(Integer, ForeignKey("messages.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    read_at = Column(DateTime(timezone=True), nullable=True)

    message = relationship("Message", back_populates="statuses")
    user = relationship("User", foreign_keys=[user_id])


# ==========================================================================
# Notifications
# ==========================================================================

class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    type = Column(Enum(NotificationType), default=NotificationType.other, nullable=False)
    message = Column(String(500), nullable=False)
    # Informal reference to the post/reel/comment/etc. this notification is
    # about, same pattern as Like.target_id — not a ForeignKey since it can
    # point at different tables depending on `type`.
    target_type = Column(String(20), nullable=True)
    target_id = Column(Integer, nullable=True)
    is_read = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", foreign_keys=[user_id])
    actor = relationship("User", foreign_keys=[actor_id])


class DeviceToken(Base):
    """An FCM push-notification device token registered to a user."""

    __tablename__ = "device_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token = Column(String(255), unique=True, index=True, nullable=False)
    platform = Column(Enum(DevicePlatform), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="device_tokens")

# ==========================================================================
# Membership & Payments
# ==========================================================================

class MembershipPlan(Base):
    __tablename__ = "membership_plans"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    description = Column(String(500), nullable=True)
    # Smallest currency unit (e.g. paise/cents) to avoid float rounding issues.
    price_amount = Column(Integer, nullable=False)
    currency = Column(String(10), nullable=False, default="INR")
    interval = Column(Enum(MembershipInterval), nullable=False, default=MembershipInterval.monthly)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    memberships = relationship("UserMembership", back_populates="plan")


class UserMembership(Base):
    """A user's current/most-recent subscription to a MembershipPlan.

    One row per user is kept up to date in place (status flips to canceled/
    expired/past_due rather than deleting rows), so `.../status` always has
    something to read even after a cancellation.
    """

    __tablename__ = "user_memberships"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    plan_id = Column(Integer, ForeignKey("membership_plans.id"), nullable=False, index=True)
    status = Column(Enum(MembershipStatus), nullable=False, default=MembershipStatus.active)
    current_period_start = Column(DateTime(timezone=True), server_default=func.now())
    current_period_end = Column(DateTime(timezone=True), nullable=True)
    # Set once the subscribing payment order is confirmed paid.
    payment_order_id = Column(Integer, ForeignKey("payment_orders.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", foreign_keys=[user_id])
    plan = relationship("MembershipPlan", back_populates="memberships")


class PaymentOrder(Base):
    """A single payment-provider order/intent. Created by
    POST /api/payments/create-order, then flipped to paid/failed by
    POST /api/payments/webhook when the provider confirms it.
    """

    __tablename__ = "payment_orders"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    plan_id = Column(Integer, ForeignKey("membership_plans.id"), nullable=True, index=True)
    provider = Column(Enum(PaymentProvider), nullable=False, default=PaymentProvider.razorpay)
    provider_order_id = Column(String(100), unique=True, index=True, nullable=False)
    amount = Column(Integer, nullable=False)
    currency = Column(String(10), nullable=False, default="INR")
    status = Column(Enum(PaymentStatus), nullable=False, default=PaymentStatus.created)
    receipt = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    paid_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    plan = relationship("MembershipPlan")


# ==========================================================================
# Discord Integration
# ==========================================================================

class DiscordLink(Base):
    """Links a platform user to their Discord account (one-to-one)."""

    __tablename__ = "discord_links"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)
    discord_user_id = Column(String(32), unique=True, index=True, nullable=False)
    discord_username = Column(String(100), nullable=True)
    linked_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", foreign_keys=[user_id])


# ==========================================================================
# Ads
# ==========================================================================

class AdImpression(Base):
    """One recorded ad view/impression, used for basic delivery analytics."""

    __tablename__ = "ad_impressions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    ad_id = Column(String(100), nullable=False, index=True)
    placement = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", foreign_keys=[user_id])
