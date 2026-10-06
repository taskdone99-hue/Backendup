"""
Upload-page settings for reels/videos (language, AI label, engagement
switches, audience, visibility, schedule) and the rules that enforce them.

Everything that decides "may this viewer see / interact with this reel?"
for the new settings lives here so the routers share one definition:

  - viewer_can_view / require_reel_viewable   -> a single reel, by id
  - visible_reels_clause                      -> list queries (feeds, profile...)
  - require_comments_open / require_comments_visible / likes visibility

Timezone convention (same as the rest of the app: everything is UTC):
  * incoming datetimes without a tz are read as UTC; aware ones are converted
  * stored as naive UTC (MySQL DATETIME has no tz; SQLite drops it)
  * returned as aware UTC (`...+00:00`)

Scheduling needs no worker for *visibility*: a reel whose schedule is still in
the future is simply filtered out at read time, and it appears on its own the
moment `scheduled_at` passes. That is restart-safe by construction. What a
worker would still be needed for is publish-time *side effects* (e.g.
notifying followers "new reel") — the project has no scheduler/worker today
(only the cron-style script app/cleanup_expired_stories.py), so none of that
is attempted here.
"""

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app import models

# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------

# Canonical English name -> ISO 639-1 code. A request may use either the name
# (any case) or the code; the canonical name is what gets stored/returned.
# Add a row here to support another language — nothing else needs to change.
_LANGUAGES: dict[str, str] = {
    "Afrikaans": "af", "Albanian": "sq", "Amharic": "am", "Arabic": "ar",
    "Armenian": "hy", "Assamese": "as", "Azerbaijani": "az", "Basque": "eu",
    "Belarusian": "be", "Bengali": "bn", "Bhojpuri": "bho", "Bosnian": "bs",
    "Bulgarian": "bg", "Burmese": "my", "Catalan": "ca", "Chinese": "zh",
    "Croatian": "hr", "Czech": "cs", "Danish": "da", "Dogri": "doi",
    "Dutch": "nl", "English": "en", "Estonian": "et", "Filipino": "fil",
    "Finnish": "fi", "French": "fr", "Galician": "gl", "Georgian": "ka",
    "German": "de", "Greek": "el", "Gujarati": "gu", "Haitian Creole": "ht",
    "Hausa": "ha", "Hebrew": "he", "Hindi": "hi", "Hungarian": "hu",
    "Icelandic": "is", "Indonesian": "id", "Irish": "ga", "Italian": "it",
    "Japanese": "ja", "Javanese": "jv", "Kannada": "kn", "Kashmiri": "ks",
    "Kazakh": "kk", "Khmer": "km", "Konkani": "kok", "Korean": "ko",
    "Lao": "lo", "Latvian": "lv", "Lithuanian": "lt", "Macedonian": "mk",
    "Maithili": "mai", "Malay": "ms", "Malayalam": "ml", "Maltese": "mt",
    "Manipuri": "mni", "Marathi": "mr", "Mongolian": "mn", "Nepali": "ne",
    "Norwegian": "no", "Odia": "or", "Pashto": "ps", "Persian": "fa",
    "Polish": "pl", "Portuguese": "pt", "Punjabi": "pa", "Romanian": "ro",
    "Russian": "ru", "Sanskrit": "sa", "Santali": "sat", "Serbian": "sr",
    "Sindhi": "sd", "Sinhala": "si", "Slovak": "sk", "Slovenian": "sl",
    "Somali": "so", "Spanish": "es", "Swahili": "sw", "Swedish": "sv",
    "Tamil": "ta", "Telugu": "te", "Thai": "th", "Turkish": "tr",
    "Ukrainian": "uk", "Urdu": "ur", "Uzbek": "uz", "Vietnamese": "vi",
    "Welsh": "cy", "Yoruba": "yo", "Zulu": "zu",
}
_LANGUAGE_LOOKUP: dict[str, str] = {}
for _name, _code in _LANGUAGES.items():
    _LANGUAGE_LOOKUP[_name.lower()] = _name
    _LANGUAGE_LOOKUP[_code.lower()] = _name


def supported_languages() -> list[str]:
    return sorted(_LANGUAGES)


def normalize_language(raw: str | None) -> str:
    """Canonical language name for `raw` (name or ISO 639-1 code, any case).
    Raises ValueError with a client-readable message if empty/unknown."""
    if raw is None or not raw.strip():
        raise ValueError("language must not be empty")
    canonical = _LANGUAGE_LOOKUP.get(raw.strip().lower())
    if canonical is None:
        raise ValueError(
            f"Unsupported language '{raw.strip()[:50]}'. Use a language name such as "
            "'English' or 'Hindi', or its ISO 639-1 code such as 'en'."
        )
    return canonical


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    """Aware UTC view of a stored/incoming datetime (naive = already UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_storage_utc(dt: datetime) -> datetime:
    """Naive-UTC form used for the DB column and for comparing against it."""
    return as_utc(dt).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Schedule state
# ---------------------------------------------------------------------------

def is_schedule_pending(reel: models.Reel, now: datetime | None = None) -> bool:
    """True while the reel is scheduled for a moment that hasn't arrived yet —
    i.e. it must not be shown to anyone but its owner/collaborators."""
    if not reel.schedule_enabled or reel.scheduled_at is None:
        return False
    return as_utc(reel.scheduled_at) > (now or utcnow())


def is_schedule_elapsed(reel: models.Reel, now: datetime | None = None) -> bool:
    """True once a schedule has fired — the reel went live on its schedule, so
    the schedule is history and can't be re-armed (see PUT .../schedule)."""
    if not reel.schedule_enabled or reel.scheduled_at is None:
        return False
    return as_utc(reel.scheduled_at) <= (now or utcnow())


# ---------------------------------------------------------------------------
# Who can see a reel
# ---------------------------------------------------------------------------

def _is_collaborator(db: Session, reel: models.Reel, viewer_id: int) -> bool:
    return (
        db.query(models.ReelCollaborator.id)
        .filter(
            models.ReelCollaborator.reel_id == reel.id,
            models.ReelCollaborator.user_id == viewer_id,
        )
        .first()
        is not None
    )


def viewer_is_member(db: Session, reel: models.Reel, viewer_id: int | None) -> bool:
    """HOOK for `visibility="members"`.

    There is no creator -> member relationship in the project: UserMembership
    is a *platform* subscription (user X pays for plan Y), not "user X
    subscribes to creator Z". Treating "has any active platform plan" as
    "is a member of this creator" would silently invent product semantics, so
    this deliberately returns False (fail closed: owner + collaborators only).
    When a creator-membership table exists, implement the lookup here AND the
    matching condition in visible_reels_clause().
    """
    return False


def viewer_can_view(db: Session, reel: models.Reel, viewer_id: int | None) -> bool:
    """Visibility + schedule check for one reel. (Block / private-account
    rules are separate — content_routes._require_author_visible.)"""
    if viewer_id is not None:
        if reel.user_id == viewer_id or _is_collaborator(db, reel, viewer_id):
            return True
    if is_schedule_pending(reel):
        return False
    visibility = reel.visibility or models.ReelVisibility.public.value
    if visibility == models.ReelVisibility.public.value:
        return True
    if visibility == models.ReelVisibility.members.value:
        return viewer_is_member(db, reel, viewer_id)
    return False  # private (or any unknown value: fail closed)


def require_reel_viewable(db: Session, reel: models.Reel, viewer_id: int | None) -> None:
    """404 — not 403 — when the viewer may not see the reel: a private or
    not-yet-published reel shouldn't even reveal that it exists (same stance
    as the block rule in content_routes._require_author_visible)."""
    if not viewer_can_view(db, reel, viewer_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reel not found")


def visible_reels_clause(db: Session, viewer_id: int | None):
    """SQL twin of viewer_can_view for list queries over models.Reel.
    Keep the two in step."""
    now = to_storage_utc(utcnow())
    live = or_(
        models.Reel.schedule_enabled.is_(False),
        models.Reel.scheduled_at.is_(None),
        models.Reel.scheduled_at <= now,
    )
    public = or_(
        models.Reel.visibility == models.ReelVisibility.public.value,
        models.Reel.visibility.is_(None),
    )
    open_to_everyone = and_(public, live)
    if viewer_id is None:
        return open_to_everyone
    collab_reel_ids = db.query(models.ReelCollaborator.reel_id).filter(
        models.ReelCollaborator.user_id == viewer_id
    )
    return or_(
        models.Reel.user_id == viewer_id,
        models.Reel.id.in_(collab_reel_ids),
        open_to_everyone,
    )


# ---------------------------------------------------------------------------
# Engagement switches
# ---------------------------------------------------------------------------

def require_comments_open(reel: models.Reel) -> None:
    """New comments/replies are refused for everyone (owner included) while
    comments are switched off. Existing comments are untouched."""
    if not reel.comments_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Comments are turned off for this reel"
        )


def require_comments_visible(reel: models.Reel, viewer_id: int | None) -> None:
    """With `hide_comments`, only the owner can read the comment thread."""
    if reel.hide_comments and viewer_id != reel.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Comments are hidden on this reel"
        )


def likes_hidden_from(reel: models.Reel, viewer_id: int | None) -> bool:
    """True when `viewer_id` must not be shown this reel's like count / likers."""
    return bool(reel.hide_like_count) and viewer_id != reel.user_id
