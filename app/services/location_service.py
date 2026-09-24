"""
Location lookup/creation for the "attach a place to a post/story" feature
(POST /api/locations, and the location_* fields on post/story creation).

This is explicitly NOT continuous user-location tracking: a Location row is
only ever created because a user attached one to a specific piece of
content, and no history of anyone's movements is kept anywhere.

This module only searches and dedupes against locations already saved by
users of this app. Searching real-world places and reverse geocoding through
an external provider lives in app/services/geocoding_service.py (exposed at
GET /api/locations/places/search and GET /api/locations/reverse-geocode).
"""

from sqlalchemy import func, or_
from sqlalchemy.orm import Session
import math

from app import models

# Decimal places to round to when de-duplicating manually-entered locations
# by coordinates (~11cm precision at 7 decimal places) — close enough that
# two attempts to tag "the same spot" collapse to one row, without being so
# coarse that genuinely distinct nearby places merge.
_COORDINATE_DEDUPE_PRECISION = 6


def find_or_create_location(
    db: Session,
    *,
    name: str,
    address: str | None = None,
    city: str | None = None,
    state: str | None = None,
    country: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    place_id: str | None = None,
) -> models.Location:
    """
    Returns an existing Location row if this same place was already saved,
    otherwise creates one. Dedup strategy, in order:
      1. Exact place_id match (most reliable — came from a places provider).
      2. Same name + same coordinates (rounded), for manual entries without
         a place_id.
      3. Otherwise, a brand new row.
    """
    if place_id:
        existing = (
            db.query(models.Location)
            .filter(models.Location.place_id == place_id)
            .first()
        )
        if existing is not None:
            return existing

    if latitude is not None and longitude is not None:
        lat_r = round(latitude, _COORDINATE_DEDUPE_PRECISION)
        lng_r = round(longitude, _COORDINATE_DEDUPE_PRECISION)
        existing = (
            db.query(models.Location)
            .filter(
                models.Location.name == name,
                func.round(models.Location.latitude, _COORDINATE_DEDUPE_PRECISION) == lat_r,
                func.round(models.Location.longitude, _COORDINATE_DEDUPE_PRECISION) == lng_r,
            )
            .first()
        )
        if existing is not None:
            return existing

    location = models.Location(
        name=name,
        address=address,
        city=city,
        state=state,
        country=country,
        latitude=latitude,
        longitude=longitude,
        place_id=place_id,
    )
    db.add(location)
    db.commit()
    db.refresh(location)
    return location


def get_location_or_404(db: Session, location_id: int) -> models.Location | None:
    return db.query(models.Location).filter(models.Location.id == location_id).first()


def search_locations(db: Session, query: str, limit: int, offset: int):
    """Substring search over saved locations' name/city/address — not a
    places-autocomplete API, since no such provider is configured (see
    module docstring). Returns (total, page) same shape as every other
    paginated list in this app."""
    like = f"%{query}%"
    base = db.query(models.Location).filter(
        or_(
            models.Location.name.ilike(like),
            models.Location.city.ilike(like),
            models.Location.address.ilike(like),
        )
    )
    total = base.count()
    rows = (
        base.order_by(models.Location.name.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return total, rows


def resolve_location_from_form(
    db: Session,
    *,
    location_id: int | None,
    location_name: str | None,
    location_address: str | None = None,
    location_city: str | None = None,
    location_state: str | None = None,
    location_country: str | None = None,
    location_latitude: float | None = None,
    location_longitude: float | None = None,
    location_place_id: str | None = None,
) -> models.Location | None:
    """
    Shared by post/story creation's multipart form fields: either attach an
    already-saved location by id, or find-or-create one from the inline
    fields, or (if nothing was provided) return None. Raises ValueError if
    location_id was given but doesn't exist, for the caller to turn into a
    404/400.
    """
    if location_id is not None:
        location = get_location_or_404(db, location_id)
        if location is None:
            raise ValueError(f"Location {location_id} not found")
        return location

    if location_name:
        return find_or_create_location(
            db,
            name=location_name,
            address=location_address,
            city=location_city,
            state=location_state,
            country=location_country,
            latitude=location_latitude,
            longitude=location_longitude,
            place_id=location_place_id,
        )

    return None


def find_nearby_locations(
    db: Session,
    *,
    latitude: float,
    longitude: float,
    radius_km: float,
    limit: int,
    offset: int = 0,
):
    """Return (total, page) of saved locations within radius, nearest first
    — same (total, page) shape as search_locations above, so the router can
    build a normal paginated response.

    Uses a portable bounding-box SQL pre-filter, then exact Haversine
    calculation in Python so MySQL and SQLite behave the same. The
    bounding-box query itself isn't offset/limited (it has to pull every
    candidate in the box to sort by true distance first) — offset/limit are
    applied after sorting, to the distance-ordered list.
    """
    earth_km = 6371.0088
    lat_delta = radius_km / 111.32
    cos_lat = max(math.cos(math.radians(latitude)), 1e-9)
    lng_delta = radius_km / (111.32 * cos_lat)

    rows = (
        db.query(models.Location)
        .filter(
            models.Location.latitude.isnot(None),
            models.Location.longitude.isnot(None),
            models.Location.latitude >= latitude - lat_delta,
            models.Location.latitude <= latitude + lat_delta,
            models.Location.longitude >= longitude - lng_delta,
            models.Location.longitude <= longitude + lng_delta,
        )
        .all()
    )

    lat1 = math.radians(latitude)
    lon1 = math.radians(longitude)
    results = []
    for location in rows:
        lat2 = math.radians(location.latitude)
        lon2 = math.radians(location.longitude)
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        )
        distance_km = earth_km * 2 * math.asin(min(1.0, math.sqrt(a)))
        if distance_km <= radius_km:
            results.append((location, distance_km))

    results.sort(key=lambda item: item[1])
    total = len(results)
    return total, results[offset:offset + limit]
