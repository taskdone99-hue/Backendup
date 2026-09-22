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
from app.services.location_service import (
    get_location_or_404,
    search_locations,
    find_nearby_locations,
)
from app.routers.content_routes import _visible_authors_clause, _to_post_detail, _to_reel_detail
from app.routers.story_routes import _active_story_query, _to_story_out

router = APIRouter(prefix="/api/locations", tags=["locations"])


@router.get("/search", response_model=schemas.PaginatedLocationSearchResponse)
def search_locations_endpoint(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Search locations already saved in this app (name/city/address
    substring match) — not a places-autocomplete API, see
    location_service module docstring for why."""
    total, rows = search_locations(db, q, limit, offset)
    return schemas.PaginatedLocationSearchResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[schemas.LocationOut.model_validate(r) for r in rows],
    )


@router.get("/nearby", response_model=schemas.NearbyLocationsResponse)
def nearby_locations(
    latitude: float = Query(...),
    longitude: float = Query(...),
    radius_km: float = Query(10, gt=0, le=500),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Find saved locations within radius, sorted nearest first."""
    if not (-90 <= latitude <= 90):
        raise HTTPException(status_code=400, detail="latitude must be between -90 and 90")
    if not (-180 <= longitude <= 180):
        raise HTTPException(status_code=400, detail="longitude must be between -180 and 180")

    rows = find_nearby_locations(
        db,
        latitude=latitude,
        longitude=longitude,
        radius_km=radius_km,
        limit=limit,
    )
    return schemas.NearbyLocationsResponse(
        items=[
            schemas.NearbyLocationOut(
                **schemas.LocationOut.model_validate(location).model_dump(),
                distance_km=round(distance_km, 3),
            )
            for location, distance_km in rows
        ]
    )


# NOTE: /search and /nearby must be registered before /{location_id} — Starlette
# matches routes in registration order.
@router.get("/{location_id}", response_model=schemas.LocationOut)
def get_location(location_id: int, db: Session = Depends(get_db)):
    location = get_location_or_404(db, location_id)
    if location is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Location not found")
    return schemas.LocationOut.model_validate(location)


@router.get("/{location_id}/posts", response_model=schemas.PaginatedPostDetailResponse)
def get_location_posts(
    location_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    """Posts tagged at this location — same visibility rule as every other
    cross-account feed in this app (content_routes._visible_authors_clause):
    public accounts' posts are visible to anyone, private accounts' posts
    only to the owner or an approved follower. A pending follow request
    never grants access — see _visible_authors_clause."""
    if get_location_or_404(db, location_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Location not found")

    viewer_id = current_user.id if current_user else None
    query = (
        db.query(models.Post)
        .join(models.User, models.Post.user_id == models.User.id)
        .filter(models.Post.location_id == location_id)
        .filter(_visible_authors_clause(db, viewer_id))
    )
    total = query.count()
    posts = query.order_by(models.Post.created_at.desc()).offset(offset).limit(limit).all()
    items = [_to_post_detail(db, p, viewer_id) for p in posts]
    return schemas.PaginatedPostDetailResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/{location_id}/reels", response_model=schemas.PaginatedReelDetailResponse)
def get_location_reels(
    location_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    """Reels tagged at this location — same visibility rule as
    GET /{location_id}/posts above."""
    if get_location_or_404(db, location_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Location not found")

    viewer_id = current_user.id if current_user else None
    query = (
        db.query(models.Reel)
        .join(models.User, models.Reel.user_id == models.User.id)
        .filter(models.Reel.location_id == location_id)
        .filter(_visible_authors_clause(db, viewer_id))
    )
    total = query.count()
    reels = query.order_by(models.Reel.created_at.desc()).offset(offset).limit(limit).all()
    items = [_to_reel_detail(db, r, viewer_id) for r in reels]
    return schemas.PaginatedReelDetailResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/{location_id}/stories", response_model=schemas.MyStoriesResponse)
def get_location_stories(
    location_id: int,
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    """Currently-active (non-expired) stories tagged at this location, same
    visibility rule as posts above. Expired stories are excluded by
    story_routes._active_story_query's expires_at filter — never returned
    even briefly before the cleanup job removes the row."""
    if get_location_or_404(db, location_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Location not found")

    viewer_id = current_user.id if current_user else None
    stories = (
        _active_story_query(db)
        .join(models.User, models.Story.user_id == models.User.id)
        .filter(models.Story.location_id == location_id)
        .filter(_visible_authors_clause(db, viewer_id))
        .order_by(models.Story.created_at.desc())
        .all()
    )
    return schemas.MyStoriesResponse(items=[_to_story_out(s, viewer_id) for s in stories])
