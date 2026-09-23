"""
Instagram-style unified search — GET /api/search?q=...&type=....

Searches across users (username/user id), songs (Audio title/artist), and
locations (Location name/city/address, via the existing
location_service.search_locations) in one call. `type=all` returns all
three groups (capped to `limit` items each, for a search-bar dropdown);
`type=users|songs|locations` returns one fully paginated list.
"""

from typing import Literal, Union

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user
from app.routers.user_routes import _is_following, _get_user_or_404
from app.services.location_service import search_locations
from app.services.search_service import search_songs, search_users

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get(
    "",
    response_model=Union[
        schemas.SearchAllResult,
        schemas.PaginatedSearchUsersResponse,
        schemas.PaginatedSearchSongsResponse,
        schemas.PaginatedLocationSearchResponse,
    ],
)
def search(
    q: str = Query(..., min_length=1, description="Search text"),
    type: Literal["all", "users", "songs", "locations"] = Query(
        "all", description="Which category to search"
    ),
    limit: int = Query(10, ge=1, le=50, description="Max results per category"),
    offset: int = Query(0, ge=0, description="Ignored when type=all"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Case-insensitive substring search.

    - `type=all` (default): grouped `{"users": [...], "songs": [...],
      "locations": [...]}`, each capped to `limit` (no `offset` — meant for
      a live search-bar dropdown, not a paginated results page).
    - `type=users` / `type=songs` / `type=locations`: one fully paginated
      list for that category, with `total`/`limit`/`offset`.
    """
    if type == "users":
        total, rows = search_users(db, q, limit, offset)
        items = []
        for user in rows:
            out = schemas.SearchUserOut.model_validate(user)
            if user.id != current_user.id:
                out.is_following = _is_following(db, current_user.id, user.id)
            items.append(out)
        return schemas.PaginatedSearchUsersResponse(total=total, limit=limit, offset=offset, items=items)

    if type == "songs":
        total, rows = search_songs(db, q, limit, offset)
        items = [schemas.SearchSongOut.model_validate(r) for r in rows]
        return schemas.PaginatedSearchSongsResponse(total=total, limit=limit, offset=offset, items=items)

    if type == "locations":
        total, rows = search_locations(db, q, limit, offset)
        items = [schemas.LocationOut.model_validate(r) for r in rows]
        return schemas.PaginatedLocationSearchResponse(total=total, limit=limit, offset=offset, items=items)

    # type == "all": grouped, capped to `limit` each, no offset.
    _, user_rows = search_users(db, q, limit, 0)
    _, song_rows = search_songs(db, q, limit, 0)
    _, location_rows = search_locations(db, q, limit, 0)

    user_items = []
    for user in user_rows:
        out = schemas.SearchUserOut.model_validate(user)
        if user.id != current_user.id:
            out.is_following = _is_following(db, current_user.id, user.id)
        user_items.append(out)

    return schemas.SearchAllResult(
        users=user_items,
        songs=[schemas.SearchSongOut.model_validate(r) for r in song_rows],
        locations=[schemas.LocationOut.model_validate(r) for r in location_rows],
    )


# ==========================================================================
# Recent search — the search bar's "Recent" list. Capped per user so it
# can't grow forever; oldest entries fall off once the cap is hit (see
# MAX_RECENT_SEARCHES below), same idea as hashtag_service's
# MAX_HASHTAGS_PER_POST cap.
# ==========================================================================

MAX_RECENT_SEARCHES = 50


@router.get("/recent", response_model=schemas.PaginatedRecentSearchResponse)
def get_recent_searches(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    query = db.query(models.RecentSearch).filter(models.RecentSearch.user_id == current_user.id)
    total = query.count()
    rows = (
        query.order_by(models.RecentSearch.created_at.desc(), models.RecentSearch.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    items = [schemas.RecentSearchOut.model_validate(r) for r in rows]
    return schemas.PaginatedRecentSearchResponse(total=total, limit=limit, offset=offset, items=items)


@router.post("/recent", response_model=schemas.RecentSearchOut, status_code=status.HTTP_201_CREATED)
def add_recent_search(
    payload: schemas.RecentSearchCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if payload.target_user_id is not None:
        _get_user_or_404(db, payload.target_user_id)
        existing = (
            db.query(models.RecentSearch)
            .filter(
                models.RecentSearch.user_id == current_user.id,
                models.RecentSearch.target_user_id == payload.target_user_id,
            )
            .first()
        )
    else:
        existing = (
            db.query(models.RecentSearch)
            .filter(
                models.RecentSearch.user_id == current_user.id,
                models.RecentSearch.query_text == payload.query_text,
            )
            .first()
        )

    if existing is not None:
        # Bump it to the top instead of leaving a stale duplicate — same
        # "re-searching something moves it back to the top" behavior as
        # Instagram's own recents list.
        db.delete(existing)
        db.commit()

    entry = models.RecentSearch(
        user_id=current_user.id,
        query_text=payload.query_text,
        target_user_id=payload.target_user_id,
    )
    db.add(entry)
    db.commit()

    oldest_ids = [
        row.id
        for row in db.query(models.RecentSearch.id)
        .filter(models.RecentSearch.user_id == current_user.id)
        .order_by(models.RecentSearch.created_at.desc(), models.RecentSearch.id.desc())
        .offset(MAX_RECENT_SEARCHES)
        .all()
    ]
    if oldest_ids:
        db.query(models.RecentSearch).filter(models.RecentSearch.id.in_(oldest_ids)).delete(
            synchronize_session=False
        )
        db.commit()

    db.refresh(entry)
    return schemas.RecentSearchOut.model_validate(entry)


@router.delete("/recent", response_model=schemas.MessageResponse)
def clear_recent_searches(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    db.query(models.RecentSearch).filter(models.RecentSearch.user_id == current_user.id).delete()
    db.commit()
    return schemas.MessageResponse(message="Recent searches cleared")
