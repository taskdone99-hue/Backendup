"""
Real-world place search and reverse geocoding through an external provider,
as opposed to app/services/location_service.py which only searches places
that users of *this* app have already saved.

Providers (chosen with the GEOCODING_PROVIDER env var):

  nominatim  (default) OpenStreetMap Nominatim. No API key needed. The public
             server (nominatim.openstreetmap.org) has a usage policy of
             max ~1 request/second and no heavy autocomplete traffic — fine
             for development, but for production point NOMINATIM_BASE_URL at
             a self-hosted or paid Nominatim-compatible service.
  google     Google Places API (New) Text Search for place search, and the
             Geocoding API for reverse geocoding. Needs GOOGLE_MAPS_API_KEY
             with both APIs enabled.

Privacy / security notes:
  * Provider keys are read from the environment only and are never returned
    to a client. Error messages raised from here are deliberately generic:
    Google's Geocoding API takes the key as a query-string parameter, and
    `requests` exception text includes the full URL.
  * Nothing here stores coordinates or queries. This is a lookup, not
    location tracking (see app/services/location_service.py). Coordinates and
    search text are not logged. Results are kept in a small in-process cache
    (memory only, keyed by rounded coordinates / normalized query, no user
    id) so repeated lookups don't hammer the provider.

Place ids are namespaced ("google:<id>", "osm:N123") so ids from different
providers can never collide in locations.place_id, which is unique.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = (3.05, 5)  # (connect, read) seconds
_CACHE_TTL_SECONDS = 600
_CACHE_MAX_ENTRIES = 500

# Column widths on models.Location — results are clipped to these so a
# provider result can be saved as-is without a DB "data too long" error.
_MAX_NAME = 150
_MAX_ADDRESS = 500
_MAX_PART = 100
_MAX_PLACE_ID = 255


class GeocodingError(Exception):
    """The provider failed, timed out, or returned something unusable."""


class GeocodingNotConfigured(GeocodingError):
    """No usable provider configuration (unknown provider / missing key)."""


@dataclass
class Place:
    place_id: str | None
    name: str
    address: str | None
    city: str | None
    state: str | None
    country: str | None
    latitude: float | None
    longitude: float | None
    provider: str


# --------------------------------------------------------------------------
# tiny TTL cache
# --------------------------------------------------------------------------

_cache: "OrderedDict[tuple, tuple[float, object]]" = OrderedDict()
_cache_lock = threading.Lock()


def _cache_get(key: tuple):
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        expires_at, value = hit
        if expires_at < time.monotonic():
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return value


def _cache_set(key: tuple, value) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, value)
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _clip(value, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _provider_name() -> str:
    return os.getenv("GEOCODING_PROVIDER", "nominatim").strip().lower()


def _language() -> str:
    return os.getenv("GEOCODING_LANGUAGE", "en").strip() or "en"


def _google_key() -> str:
    key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
    if not key:
        raise GeocodingNotConfigured("Places provider is not configured")
    return key


def _request_json(method: str, url: str, *, provider: str, **kwargs):
    try:
        resp = requests.request(method, url, timeout=_TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        # Log only the exception class — see module docstring re: keys in URLs.
        logger.warning("%s request failed: %s", provider, type(exc).__name__)
        raise GeocodingError("Places provider is unreachable") from None

    if resp.status_code == 429:
        logger.warning("%s rate limit hit", provider)
        raise GeocodingError("Places provider rate limit reached, try again shortly")
    if resp.status_code >= 400:
        logger.warning("%s returned HTTP %s", provider, resp.status_code)
        raise GeocodingError("Places provider returned an error")
    try:
        return resp.json()
    except ValueError:
        raise GeocodingError("Places provider returned an unreadable response") from None


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Nominatim
# --------------------------------------------------------------------------

def _nominatim_base() -> str:
    return os.getenv("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org").rstrip("/")


def _nominatim_headers() -> dict:
    # Nominatim's usage policy requires an identifying User-Agent.
    return {
        "User-Agent": os.getenv("GEOCODING_USER_AGENT", "instagram-style-backend/1.0"),
        "Accept-Language": _language(),
    }


def _nominatim_to_place(item: dict) -> Place:
    addr = item.get("address") or {}
    display = item.get("display_name") or ""

    osm_type = (item.get("osm_type") or "")[:1].upper()
    osm_id = item.get("osm_id")
    place_id = f"osm:{osm_type}{osm_id}" if osm_type and osm_id is not None else None

    name = (
        item.get("name")
        or addr.get("road")
        or (display.split(",")[0] if display else "")
    )
    city = next(
        (
            addr[k]
            for k in ("city", "town", "village", "municipality", "state_district", "county")
            if addr.get(k)
        ),
        None,
    )
    return Place(
        place_id=_clip(place_id, _MAX_PLACE_ID),
        name=_clip(name, _MAX_NAME) or "Unnamed place",
        address=_clip(display, _MAX_ADDRESS),
        city=_clip(city, _MAX_PART),
        state=_clip(addr.get("state"), _MAX_PART),
        country=_clip(addr.get("country"), _MAX_PART),
        latitude=_to_float(item.get("lat")),
        longitude=_to_float(item.get("lon")),
        provider="nominatim",
    )


def _nominatim_search(query, latitude, longitude, limit) -> list[Place]:
    params = {
        "q": query,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": limit,
    }
    if latitude is not None and longitude is not None:
        # Prefer (not require) results within ~0.5 degrees of the caller.
        params["viewbox"] = (
            f"{longitude - 0.5},{latitude + 0.5},{longitude + 0.5},{latitude - 0.5}"
        )
        params["bounded"] = 0
    data = _request_json(
        "GET", f"{_nominatim_base()}/search",
        provider="nominatim", params=params, headers=_nominatim_headers(),
    )
    if not isinstance(data, list):
        raise GeocodingError("Places provider returned an unexpected response")
    try:
        return [_nominatim_to_place(item) for item in data]
    except (AttributeError, TypeError):
        raise GeocodingError("Places provider returned an unexpected response") from None


def _nominatim_reverse(latitude, longitude) -> Place | None:
    data = _request_json(
        "GET", f"{_nominatim_base()}/reverse",
        provider="nominatim",
        params={"lat": latitude, "lon": longitude, "format": "jsonv2",
                "addressdetails": 1, "zoom": 18},
        headers=_nominatim_headers(),
    )
    if not isinstance(data, dict):
        raise GeocodingError("Places provider returned an unexpected response")
    if data.get("error"):
        # Nominatim answers 200 + {"error": "Unable to geocode"} for e.g. open ocean.
        return None
    try:
        place = _nominatim_to_place(data)
    except (AttributeError, TypeError):
        raise GeocodingError("Places provider returned an unexpected response") from None
    if place.latitude is None or place.longitude is None:
        place.latitude, place.longitude = latitude, longitude
    return place


# --------------------------------------------------------------------------
# Google
# --------------------------------------------------------------------------

_GOOGLE_PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
_GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

# Most specific first.
_GOOGLE_NAME_TYPES = (
    "premise", "point_of_interest", "establishment", "neighborhood",
    "sublocality_level_1", "sublocality", "locality",
)


def _google_component(components: list[dict], wanted: tuple[str, ...], long_key: str):
    for want in wanted:
        for comp in components:
            if want in (comp.get("types") or []) and comp.get(long_key):
                return comp[long_key]
    return None


def _google_parts(components: list[dict], long_key: str):
    city = _google_component(
        components,
        ("locality", "postal_town", "administrative_area_level_3", "administrative_area_level_2"),
        long_key,
    )
    state = _google_component(components, ("administrative_area_level_1",), long_key)
    country = _google_component(components, ("country",), long_key)
    return city, state, country


def _google_search(query, latitude, longitude, limit) -> list[Place]:
    body: dict = {
        "textQuery": query,
        "pageSize": limit,
        "languageCode": _language(),
    }
    if latitude is not None and longitude is not None:
        body["locationBias"] = {
            "circle": {
                "center": {"latitude": latitude, "longitude": longitude},
                "radius": 50000.0,
            }
        }
    data = _request_json(
        "POST", _GOOGLE_PLACES_SEARCH_URL,
        provider="google", json=body,
        headers={
            "X-Goog-Api-Key": _google_key(),
            "X-Goog-FieldMask": (
                "places.id,places.displayName,places.formattedAddress,"
                "places.location,places.addressComponents"
            ),
        },
    )
    if not isinstance(data, dict):
        raise GeocodingError("Places provider returned an unexpected response")
    places = []
    try:
        for item in data.get("places") or []:
            city, state, country = _google_parts(item.get("addressComponents") or [], "longText")
            loc = item.get("location") or {}
            pid = item.get("id")
            places.append(Place(
                place_id=_clip(f"google:{pid}", _MAX_PLACE_ID) if pid else None,
                name=_clip((item.get("displayName") or {}).get("text"), _MAX_NAME) or "Unnamed place",
                address=_clip(item.get("formattedAddress"), _MAX_ADDRESS),
                city=_clip(city, _MAX_PART),
                state=_clip(state, _MAX_PART),
                country=_clip(country, _MAX_PART),
                latitude=_to_float(loc.get("latitude")),
                longitude=_to_float(loc.get("longitude")),
                provider="google",
            ))
    except (AttributeError, TypeError):
        raise GeocodingError("Places provider returned an unexpected response") from None
    return places


def _google_reverse(latitude, longitude) -> Place | None:
    data = _request_json(
        "GET", _GOOGLE_GEOCODE_URL,
        provider="google",
        params={"latlng": f"{latitude},{longitude}", "language": _language(), "key": _google_key()},
    )
    if not isinstance(data, dict):
        raise GeocodingError("Places provider returned an unexpected response")
    status = data.get("status")
    if status == "ZERO_RESULTS":
        return None
    if status != "OK":
        # OVER_QUERY_LIMIT / REQUEST_DENIED / INVALID_REQUEST / UNKNOWN_ERROR
        logger.warning("google geocode status %s", status)
        raise GeocodingError("Places provider returned an error")
    try:
        results = data.get("results") or []
        if not results:
            return None
        top = results[0]
        components = top.get("address_components") or []
        city, state, country = _google_parts(components, "long_name")
        formatted = top.get("formatted_address")
        name = (
            _google_component(components, _GOOGLE_NAME_TYPES, "long_name")
            or (formatted or "").split(",")[0]
        )
        loc = (top.get("geometry") or {}).get("location") or {}
        pid = top.get("place_id")
        return Place(
            place_id=_clip(f"google:{pid}", _MAX_PLACE_ID) if pid else None,
            name=_clip(name, _MAX_NAME) or "Unnamed place",
            address=_clip(formatted, _MAX_ADDRESS),
            city=_clip(city, _MAX_PART),
            state=_clip(state, _MAX_PART),
            country=_clip(country, _MAX_PART),
            latitude=_to_float(loc.get("lat")) if loc.get("lat") is not None else latitude,
            longitude=_to_float(loc.get("lng")) if loc.get("lng") is not None else longitude,
            provider="google",
        )
    except (AttributeError, TypeError):
        raise GeocodingError("Places provider returned an unexpected response") from None


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

_SEARCH = {"nominatim": _nominatim_search, "google": _google_search}
_REVERSE = {"nominatim": _nominatim_reverse, "google": _google_reverse}


def _resolve_provider() -> str:
    name = _provider_name()
    if name not in _SEARCH:
        raise GeocodingNotConfigured("Places provider is not configured")
    return name


def search_places(
    query: str,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    limit: int = 5,
) -> list[Place]:
    """Search real-world places by free text. If latitude/longitude are given,
    results near that point are preferred (a bias, not a hard filter)."""
    provider = _resolve_provider()
    if provider == "google":
        _google_key()  # fail fast (and never cache) when the key is missing

    key = (
        "search", provider, query.strip().lower(), limit,
        None if latitude is None else round(latitude, 2),
        None if longitude is None else round(longitude, 2),
    )
    cached = _cache_get(key)
    if cached is not None:
        return list(cached)

    places = _SEARCH[provider](query.strip(), latitude, longitude, limit)
    _cache_set(key, list(places))
    return places


def reverse_geocode(latitude: float, longitude: float) -> Place | None:
    """Turn coordinates into the address / place name there. Returns None if
    the provider knows of nothing at that point."""
    provider = _resolve_provider()
    if provider == "google":
        _google_key()

    key = ("reverse", provider, round(latitude, 4), round(longitude, 4))
    cached = _cache_get(key)
    if cached is not None:
        # A cached miss is stored as False (None means "not in cache").
        return cached or None

    place = _REVERSE[provider](latitude, longitude)
    _cache_set(key, place if place is not None else False)
    return place
