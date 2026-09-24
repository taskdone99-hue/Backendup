"""
Location APIs — attach a place to a post/story/reel, look it up, search
saved locations, and browse content tagged at a location.

Not user-location tracking: every Location row exists only because someone
explicitly attached it to a post/story/reel — see
app/services/location_service.py.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user, get_current_user_optional
from app.services import geocoding_service
from app.services.location_service import (
    find_or_create_location,
    get_location_or_404,
    search_locations,
    find_nearby_locations,
)
from app.routers.content_routes import (
    _visible_authors_clause,
    _to_post_detail,
    _to_reel_detail,
)
from app.routers.story_routes import _active_story_query, _to_story_out


router = APIRouter(prefix="/api/locations", tags=["locations"])


@router.post(
    "",
    response_model=schemas.LocationOut,
    status_code=status.HTTP_201_CREATED,
)
def create_location(
    payload: schemas.LocationCreate,
    db: Session = Depends(get_db),
):
    # Reuses an existing row for the same place_id (e.g. a result from
    # /places/search picked by several users) or same name+coordinates,
    # instead of inserting a duplicate each time.
    location = find_or_create_location(
        db,
        name=payload.name,
        address=payload.address,
        city=payload.city,
        state=payload.state,
        country=payload.country,
        latitude=payload.latitude,
        longitude=payload.longitude,
        place_id=payload.place_id,
    )

    return schemas.LocationOut.model_validate(location)


@router.get(
    "/search",
    response_model=schemas.PaginatedLocationSearchResponse,
)
def search_locations_endpoint(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Search locations already saved in this app."""

    total, rows = search_locations(db, q, limit, offset)

    return schemas.PaginatedLocationSearchResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[
            schemas.LocationOut.model_validate(r)
            for r in rows
        ],
    )


@router.get(
    "/nearby",
    response_model=schemas.NearbyLocationsResponse,
)
def nearby_locations(
    latitude: float = Query(...),
    longitude: float = Query(...),
    radius_km: float = Query(10, gt=0, le=500),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Find saved locations within radius, sorted nearest first."""

    if not (-90 <= latitude <= 90):
        raise HTTPException(
            status_code=400,
            detail="latitude must be between -90 and 90",
        )

    if not (-180 <= longitude <= 180):
        raise HTTPException(
            status_code=400,
            detail="longitude must be between -180 and 180",
        )

    total, rows = find_nearby_locations(
        db,
        latitude=latitude,
        longitude=longitude,
        radius_km=radius_km,
        limit=limit,
        offset=offset,
    )

    return schemas.NearbyLocationsResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[
            schemas.NearbyLocationOut(
                **schemas.LocationOut.model_validate(location).model_dump(),
                distance_km=round(distance_km, 3),
            )
            for location, distance_km in rows
        ],
    )


def _raise_for_geocoding_error(exc: geocoding_service.GeocodingError):
    if isinstance(exc, geocoding_service.GeocodingNotConfigured):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Place lookup is not available right now",
        )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Place lookup failed, please try again",
    )


@router.get(
    "/places/search",
    response_model=schemas.PlaceSearchResponse,
)
def search_places_endpoint(
    q: str = Query(..., min_length=2, max_length=200),
    latitude: float | None = Query(None, ge=-90, le=90),
    longitude: float | None = Query(None, ge=-180, le=180),
    limit: int = Query(5, ge=1, le=10),
    current_user: models.User = Depends(get_current_user),
):
    """Search real-world places through the configured places provider (not
    just locations already saved in this app — that's GET /search).

    Pass latitude+longitude to prefer results near the caller. Login is
    required because every call spends the provider quota. Nothing about
    the query or coordinates is stored.
    """

    if (latitude is None) != (longitude is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="latitude and longitude must be provided together",
        )

    query = q.strip()
    if len(query) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="q must be at least 2 characters",
        )

    try:
        places = geocoding_service.search_places(
            query, latitude=latitude, longitude=longitude, limit=limit
        )
    except geocoding_service.GeocodingError as exc:
        _raise_for_geocoding_error(exc)

    return schemas.PlaceSearchResponse(
        items=[schemas.PlaceOut(**vars(p)) for p in places]
    )


@router.get(
    "/reverse-geocode",
    response_model=schemas.PlaceOut,
)
def reverse_geocode_endpoint(
    latitude: float = Query(..., ge=-90, le=90),
    longitude: float = Query(..., ge=-180, le=180),
    current_user: models.User = Depends(get_current_user),
):
    """Turn coordinates into the address / place name at that point (for
    "use my current location"). One-shot lookup: the coordinates are not
    saved or logged, and no history of them is kept.
    """

    try:
        place = geocoding_service.reverse_geocode(latitude, longitude)
    except geocoding_service.GeocodingError as exc:
        _raise_for_geocoding_error(exc)

    if place is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No place found at these coordinates",
        )

    return schemas.PlaceOut(**vars(place))


# NOTE: /search, /nearby, /places/search and /reverse-geocode must be
# registered before /{location_id}.
@router.get(
    "/{location_id}",
    response_model=schemas.LocationOut,
)
def get_location(
    location_id: int,
    db: Session = Depends(get_db),
):
    location = get_location_or_404(db, location_id)

    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Location not found",
        )

    return schemas.LocationOut.model_validate(location)


@router.get(
    "/{location_id}/posts",
    response_model=schemas.PaginatedPostDetailResponse,
)
def get_location_posts(
    location_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    """Posts tagged at this location."""

    if get_location_or_404(db, location_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Location not found",
        )

    viewer_id = current_user.id if current_user else None

    query = (
        db.query(models.Post)
        .join(models.User, models.Post.user_id == models.User.id)
        .filter(models.Post.location_id == location_id)
        .filter(_visible_authors_clause(db, viewer_id))
    )

    total = query.count()

    posts = (
        query
        .order_by(models.Post.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    items = [
        _to_post_detail(db, p, viewer_id)
        for p in posts
    ]

    return schemas.PaginatedPostDetailResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=items,
    )


@router.get(
    "/{location_id}/reels",
    response_model=schemas.PaginatedReelDetailResponse,
)
def get_location_reels(
    location_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    """Reels tagged at this location."""

    if get_location_or_404(db, location_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Location not found",
        )

    viewer_id = current_user.id if current_user else None

    query = (
        db.query(models.Reel)
        .join(models.User, models.Reel.user_id == models.User.id)
        .filter(models.Reel.location_id == location_id)
        .filter(_visible_authors_clause(db, viewer_id))
    )

    total = query.count()

    reels = (
        query
        .order_by(models.Reel.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    items = [
        _to_reel_detail(db, r, viewer_id)
        for r in reels
    ]

    return schemas.PaginatedReelDetailResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=items,
    )


@router.get(
    "/{location_id}/stories",
    response_model=schemas.MyStoriesResponse,
)
def get_location_stories(
    location_id: int,
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    """Currently-active stories tagged at this location."""

    if get_location_or_404(db, location_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Location not found",
        )

    viewer_id = current_user.id if current_user else None

    stories = (
        _active_story_query(db)
        .join(models.User, models.Story.user_id == models.User.id)
        .filter(models.Story.location_id == location_id)
        .filter(_visible_authors_clause(db, viewer_id))
        .order_by(models.Story.created_at.desc())
        .all()
    )

    return schemas.MyStoriesResponse(
        items=[
            _to_story_out(s, viewer_id)
            for s in stories
        ]
    )