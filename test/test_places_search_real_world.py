"""
GET /api/locations/places/search exercised against realistic real-world
payloads for Hyderabad, Nalgonda and Charminar, with the caller's
latitude/longitude present ("location ON", nearby-bias applied) and absent
("location OFF", plain text search) — both providers.

Real outbound calls to nominatim.openstreetmap.org / Google aren't made
here: this container's network egress doesn't allow reaching either host
(see the module docstring in geocoding_service.py), and hitting a public
third-party API from every CI run would be flaky and rate-limited by
policy anyway. Instead, `requests.request` is faked to return the exact
shape each provider's live API returns for these three places (captured
from their public docs / a manual lookup), so what's under test is our
request-building, response-mapping and the route — the same approach
test_places_geocoding.py already uses. This endpoint deliberately never
touches /api/locations/search (that's app/services/location_service.py —
only searches locations already saved in this app); nothing here imports
or calls into it.
"""

import pytest

from app.services import geocoding_service as gs


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    gs.clear_cache()
    monkeypatch.delenv("GEOCODING_PROVIDER", raising=False)
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    monkeypatch.delenv("NOMINATIM_BASE_URL", raising=False)
    yield
    gs.clear_cache()


@pytest.fixture()
def fake_http(monkeypatch):
    calls = []
    state = {"respond": lambda method, url, **kw: FakeResponse([])}

    def _request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return state["respond"](method, url, **kwargs)

    monkeypatch.setattr(gs.requests, "request", _request)
    return type("F", (), {"calls": calls, "state": state})


# Real-shape Nominatim jsonv2 results for each place (trimmed to the fields
# our mapping actually reads).
NOMINATIM_RESULTS = {
    "Hyderabad": [{
        "osm_type": "relation", "osm_id": 7880477,
        "lat": "17.3850440", "lon": "78.4866710",
        "name": "Hyderabad",
        "display_name": "Hyderabad, Telangana, India",
        "address": {"city": "Hyderabad", "state": "Telangana", "country": "India"},
    }],
    "Nalgonda": [{
        "osm_type": "relation", "osm_id": 9199823,
        "lat": "17.0575", "lon": "79.2685",
        "name": "Nalgonda",
        "display_name": "Nalgonda, Telangana, India",
        "address": {"city": "Nalgonda", "state": "Telangana", "country": "India"},
    }],
    "Charminar": [{
        "osm_type": "way", "osm_id": 34674032,
        "lat": "17.3616", "lon": "78.4747",
        "name": "Charminar",
        "display_name": "Charminar, Char Kaman, Hyderabad, Telangana, 500002, India",
        "address": {
            "tourism": "Charminar", "road": "Char Kaman",
            "city": "Hyderabad", "state": "Telangana", "country": "India",
        },
    }],
}

# A caller located roughly in Hyderabad, for the "location ON" cases.
CALLER_LAT, CALLER_LNG = 17.4, 78.45


@pytest.mark.parametrize("place", ["Hyderabad", "Nalgonda", "Charminar"])
class TestNominatimRealWorldPlaces:
    """Default provider (nominatim), no API key required."""

    def test_location_on_biases_search_near_caller(self, fake_http, place):
        fake_http.state["respond"] = lambda *a, **k: FakeResponse(NOMINATIM_RESULTS[place])

        results = gs.search_places(place, latitude=CALLER_LAT, longitude=CALLER_LNG, limit=5)

        assert len(results) == 1
        found = results[0]
        assert found.name == place
        assert found.provider == "nominatim"
        assert found.place_id is not None
        assert found.latitude is not None and found.longitude is not None

        call = fake_http.calls[0]
        assert call["params"]["q"] == place
        # bias present, not a hard filter (bounded=0)
        assert "viewbox" in call["params"] and call["params"]["bounded"] == 0

    def test_location_off_searches_by_query_alone(self, fake_http, place):
        fake_http.state["respond"] = lambda *a, **k: FakeResponse(NOMINATIM_RESULTS[place])

        results = gs.search_places(place, limit=5)

        assert len(results) == 1 and results[0].name == place
        call = fake_http.calls[0]
        assert call["params"]["q"] == place
        assert "viewbox" not in call["params"]


@pytest.mark.parametrize("place", ["Hyderabad", "Nalgonda", "Charminar"])
class TestGoogleRealWorldPlaces:
    """Same three places through the Google Places (New) provider."""

    def _google_body(self, place):
        item = NOMINATIM_RESULTS[place][0]
        return {"places": [{
            "id": f"ChIJ_{place}",
            "displayName": {"text": place, "languageCode": "en"},
            "formattedAddress": item["display_name"],
            "location": {"latitude": float(item["lat"]), "longitude": float(item["lon"])},
            "addressComponents": [
                {"longText": "Telangana", "types": ["administrative_area_level_1"]},
                {"longText": "India", "types": ["country"]},
            ],
        }]}

    def test_location_on_sends_location_bias(self, fake_http, monkeypatch, place):
        monkeypatch.setenv("GEOCODING_PROVIDER", "google")
        monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
        fake_http.state["respond"] = lambda *a, **k: FakeResponse(self._google_body(place))

        results = gs.search_places(place, latitude=CALLER_LAT, longitude=CALLER_LNG, limit=5)

        assert len(results) == 1 and results[0].name == place and results[0].provider == "google"
        call = fake_http.calls[0]
        assert call["json"]["textQuery"] == place
        assert call["json"]["locationBias"]["circle"]["center"] == {
            "latitude": CALLER_LAT, "longitude": CALLER_LNG,
        }

    def test_location_off_omits_location_bias(self, fake_http, monkeypatch, place):
        monkeypatch.setenv("GEOCODING_PROVIDER", "google")
        monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
        fake_http.state["respond"] = lambda *a, **k: FakeResponse(self._google_body(place))

        results = gs.search_places(place, limit=5)

        assert len(results) == 1 and results[0].name == place
        assert "locationBias" not in fake_http.calls[0]["json"]


# ------------------------------------------------------------- via the route

@pytest.mark.parametrize("place,lat,lng", [
    ("Hyderabad", None, None),
    ("Hyderabad", CALLER_LAT, CALLER_LNG),
    ("Nalgonda", None, None),
    ("Nalgonda", CALLER_LAT, CALLER_LNG),
    ("Charminar", None, None),
    ("Charminar", CALLER_LAT, CALLER_LNG),
])
def test_places_search_route_real_world_places(client, make_user, fake_http, place, lat, lng):
    client.login(make_user())
    fake_http.state["respond"] = lambda *a, **k: FakeResponse(NOMINATIM_RESULTS[place])

    params = {"q": place}
    if lat is not None:
        params["latitude"] = lat
        params["longitude"] = lng

    r = client.get("/api/locations/places/search", params=params)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1 and items[0]["name"] == place
    assert items[0]["place_id"].startswith("osm:")


# --------------------------------------------------- empty results != error

def test_zero_matches_is_a_normal_empty_list_not_an_error(client, make_user, fake_http):
    """A query with genuinely no matches (nominatim answers 200 + []) must
    come back as a plain empty result set, never a 502/503 — that's the
    distinction between "the provider is broken" and "nothing matched"."""
    client.login(make_user())
    fake_http.state["respond"] = lambda *a, **k: FakeResponse([])

    r = client.get(
        "/api/locations/places/search",
        params={"q": "zzznonexistentplacezzz", "latitude": CALLER_LAT, "longitude": CALLER_LNG},
    )
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_zero_matches_without_location_is_also_not_an_error(client, make_user, fake_http):
    client.login(make_user())
    fake_http.state["respond"] = lambda *a, **k: FakeResponse([])

    r = client.get("/api/locations/places/search", params={"q": "zzznonexistentplacezzz"})
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_actual_provider_error_still_surfaces_as_502_not_empty(client, make_user, fake_http):
    """The flip side: a *real* upstream failure must not be swallowed into
    a misleadingly-empty result — the caller needs to know to retry."""
    client.login(make_user())
    fake_http.state["respond"] = lambda *a, **k: FakeResponse({}, status_code=500)

    r = client.get("/api/locations/places/search", params={"q": "Hyderabad"})
    assert r.status_code == 502
    assert r.json().get("detail") or r.json().get("message")  # a real error body, not {"items": []}


def test_places_search_never_touches_saved_locations_service(client, make_user, fake_http, monkeypatch):
    """Guards the "do not depend on /api/locations/search" requirement: even
    with the saved-locations search broken, /places/search must still work
    — proof the provider (fake_http), not a DB lookup of saved Locations,
    is what's answering."""
    import app.routers.location_routes as location_routes

    def _boom(*a, **k):
        raise AssertionError("search_locations (saved-locations search) must not be called")

    monkeypatch.setattr(location_routes, "search_locations", _boom)

    client.login(make_user())
    fake_http.state["respond"] = lambda *a, **k: FakeResponse(NOMINATIM_RESULTS["Hyderabad"])
    r = client.get("/api/locations/places/search", params={"q": "Hyderabad"})
    assert r.status_code == 200 and r.json()["items"][0]["name"] == "Hyderabad"
