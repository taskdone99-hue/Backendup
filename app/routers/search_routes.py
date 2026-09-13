"""
Instagram-style unified search — GET /api/search?q=...&type=....

Searches across users (username/user id), songs (Audio title/artist), and
locations (Location name/city/address, via the existing
location_service.search_locations) in one call. `type=all` returns all
three groups (capped to `limit` items each, for a search-bar dropdown);
`type=users|songs|locations` returns one fully paginated list.
"""

from typing import Literal, Union

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user
from app.routers.user_routes import _is_following
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
