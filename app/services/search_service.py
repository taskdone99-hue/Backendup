"""
Backing queries for the unified search bar (GET /api/search).

Reuses existing models rather than introducing new ones:
  - "Users"     -> models.User            (username / full_name / numeric id)
  - "Songs"     -> models.Audio           (title / artist) — the existing
                    "saveable sound" table (see its docstring in models.py);
                    there's no separate Song model anywhere in this codebase.
  - "Locations" -> models.Location, via app.services.location_service's
                    existing search_locations(), unchanged.
  - "Hashtags"  -> models.Hashtag         (name) — the same table that
                    app/routers/hashtag_routes.py serves; per-hashtag post
                    counts come from hashtag_service.hashtag_posts_count.

All three are simple case-insensitive substring (ILIKE) matches, which
MySQL's default collation for these columns already treats as
case-insensitive — no extra normalization needed.
"""

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import models


def search_users(db: Session, query: str, limit: int, offset: int):
    """Case-insensitive substring match on username/full_name, plus an exact
    id match when the query is purely numeric ("Users by username/user ID").
    Only active accounts are returned."""
    like = f"%{query}%"
    filters = [models.User.username.ilike(like), models.User.full_name.ilike(like)]
    if query.strip().isdigit():
        filters.append(models.User.id == int(query.strip()))

    base = db.query(models.User).filter(models.User.is_active.is_(True)).filter(or_(*filters))
    total = base.count()
    rows = base.order_by(models.User.username.asc()).offset(offset).limit(limit).all()
    return total, rows


def search_songs(db: Session, query: str, limit: int, offset: int):
    """Case-insensitive substring match on an audio track's title or
    artist."""
    like = f"%{query}%"
    base = db.query(models.Audio).filter(
        or_(models.Audio.title.ilike(like), models.Audio.artist.ilike(like))
    )
    total = base.count()
    rows = base.order_by(models.Audio.title.asc()).offset(offset).limit(limit).all()
    return total, rows


def search_hashtags(db: Session, query: str, limit: int, offset: int):
    """Case-insensitive substring match on a hashtag's name. A leading '#'
    in the query is ignored ("#anjali" == "anjali"), same normalization as
    hashtag_routes (names are stored lowercase, without the '#').

    `%` / `_` in the query are matched literally (autoescape) — '_' is common
    inside hashtag names, so it must not act as a single-char wildcard."""
    term = query.strip().lstrip("#").lower()
    if not term:  # e.g. q="#": nothing meaningful to match, don't match everything
        return 0, []

    base = db.query(models.Hashtag).filter(models.Hashtag.name.contains(term, autoescape=True))
    total = base.count()
    rows = base.order_by(models.Hashtag.name.asc()).offset(offset).limit(limit).all()
    return total, rows
