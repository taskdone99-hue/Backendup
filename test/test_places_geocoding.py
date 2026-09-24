"""
Tests for real-world place search (GET /api/locations/places/search) and
reverse geocoding (GET /api/locations/reverse-geocode).

The provider's HTTP layer (requests.request) is faked — these tests verify
our request building, response mapping, error handling and routes, NOT that
Google/Nominatim are reachable from CI.
"""

import pytest
import requests

from app import models
from app.services import geocoding_service as gs


class FakeResponse:
    def __init__(self, payload=None, status_code=200, bad_json=False):
        self._payload = payload
        self.status_code = status_code
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
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
    """Records every provider call and replies with whatever `respond` returns."""
    calls = []
    state = {"respond": lambda method, url, **kw: FakeResponse({})}

    def _request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return state["respond"](method, url, **kwargs)

    monkeypatch.setattr(gs.requests, "request", _request)
    return type("F", (), {"calls": calls, "state": state})


NOMINATIM_ITEM = {
    "osm_type": "node",
    "osm_id": 123456,
    "lat": "17.3616",
    "lon": "78.4747",
    "name": "Charminar",
    "display_name": "Charminar, Char Kaman, Hyderabad, Telangana, 500002, India",
    "address": {
        "tourism": "Charminar",
        "road": "Char Kaman",
        "city": "Hyderabad",
        "state": "Telangana",
        "country": "India",
    },
}


# ---------------------------------------------------------------- Nominatim

def test_nominatim_search_maps_results(fake_http):
    fake_http.state["respond"] = lambda *a, **k: FakeResponse([NOMINATIM_ITEM])
    places = gs.search_places("charminar", latitude=17.4, longitude=78.5, limit=3)

    assert len(places) == 1
    p = places[0]
    assert p.place_id == "osm:N123456"
    assert (p.name, p.city, p.state, p.country) == ("Charminar", "Hyderabad", "Telangana", "India")
    assert p.latitude == pytest.approx(17.3616) and p.longitude == pytest.approx(78.4747)
    assert p.provider == "nominatim"

    call = fake_http.calls[0]
    assert call["url"].endswith("/search")
    assert call["params"]["q"] == "charminar" and call["params"]["limit"] == 3
    assert "viewbox" in call["params"] and call["params"]["bounded"] == 0
    assert "User-Agent" in call["headers"]


def test_nominatim_search_without_bias_has_no_viewbox(fake_http):
    fake_http.state["respond"] = lambda *a, **k: FakeResponse([])
    assert gs.search_places("nowhere") == []
    assert "viewbox" not in fake_http.calls[0]["params"]


def test_nominatim_reverse_maps_result_and_no_result(fake_http):
    fake_http.state["respond"] = lambda *a, **k: FakeResponse(NOMINATIM_ITEM)
    place = gs.reverse_geocode(17.3616, 78.4747)
    assert place.name == "Charminar" and place.address.startswith("Charminar,")
    assert fake_http.calls[0]["url"].endswith("/reverse")

    gs.clear_cache()
    fake_http.state["respond"] = lambda *a, **k: FakeResponse({"error": "Unable to geocode"})
    assert gs.reverse_geocode(0.0, 0.0) is None


def test_nominatim_reverse_name_falls_back_to_road(fake_http):
    item = {**NOMINATIM_ITEM, "name": "", "display_name": "12, Char Kaman, Hyderabad, India"}
    fake_http.state["respond"] = lambda *a, **k: FakeResponse(item)
    assert gs.reverse_geocode(17.3616, 78.4747).name == "Char Kaman"


# ------------------------------------------------------------------- Google

GOOGLE_PLACES_BODY = {
    "places": [{
        "id": "ChIJabc",
        "displayName": {"text": "Charminar", "languageCode": "en"},
        "formattedAddress": "Char Kaman, Hyderabad, Telangana 500002, India",
        "location": {"latitude": 17.3616, "longitude": 78.4747},
        "addressComponents": [
            {"longText": "Hyderabad", "shortText": "Hyderabad", "types": ["locality", "political"]},
            {"longText": "Telangana", "shortText": "TG", "types": ["administrative_area_level_1"]},
            {"longText": "India", "shortText": "IN", "types": ["country"]},
        ],
    }]
}

GOOGLE_GEOCODE_BODY = {
    "status": "OK",
    "results": [{
        "place_id": "ChIJxyz",
        "formatted_address": "Char Kaman, Hyderabad, Telangana 500002, India",
        "geometry": {"location": {"lat": 17.3616, "lng": 78.4747}},
        "address_components": [
            {"long_name": "Charminar", "types": ["point_of_interest", "establishment"]},
            {"long_name": "Hyderabad", "types": ["locality"]},
            {"long_name": "Telangana", "types": ["administrative_area_level_1"]},
            {"long_name": "India", "types": ["country"]},
        ],
    }],
}


def test_google_search_request_and_mapping(fake_http, monkeypatch):
    monkeypatch.setenv("GEOCODING_PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    fake_http.state["respond"] = lambda *a, **k: FakeResponse(GOOGLE_PLACES_BODY)

    places = gs.search_places("charminar", latitude=17.4, longitude=78.5, limit=4)

    p = places[0]
    assert p.place_id == "google:ChIJabc" and p.name == "Charminar"
    assert (p.city, p.state, p.country) == ("Hyderabad", "Telangana", "India")
    assert p.provider == "google"

    call = fake_http.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/v1/places:searchText")
    assert call["headers"]["X-Goog-Api-Key"] == "test-key"
    assert "places.id" in call["headers"]["X-Goog-FieldMask"]
    assert call["json"]["textQuery"] == "charminar" and call["json"]["pageSize"] == 4
    assert call["json"]["locationBias"]["circle"]["center"] == {"latitude": 17.4, "longitude": 78.5}


def test_google_reverse_mapping(fake_http, monkeypatch):
    monkeypatch.setenv("GEOCODING_PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    fake_http.state["respond"] = lambda *a, **k: FakeResponse(GOOGLE_GEOCODE_BODY)

    p = gs.reverse_geocode(17.3616, 78.4747)
    assert p.place_id == "google:ChIJxyz" and p.name == "Charminar" and p.city == "Hyderabad"
    assert fake_http.calls[0]["params"]["latlng"] == "17.3616,78.4747"


def test_google_reverse_zero_results_and_error_statuses(fake_http, monkeypatch):
    monkeypatch.setenv("GEOCODING_PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")

    fake_http.state["respond"] = lambda *a, **k: FakeResponse({"status": "ZERO_RESULTS", "results": []})
    assert gs.reverse_geocode(0.0, 0.0) is None

    gs.clear_cache()
    fake_http.state["respond"] = lambda *a, **k: FakeResponse({"status": "REQUEST_DENIED"})
    with pytest.raises(gs.GeocodingError):
        gs.reverse_geocode(1.0, 1.0)


def test_google_without_key_is_not_configured_and_makes_no_call(fake_http, monkeypatch):
    monkeypatch.setenv("GEOCODING_PROVIDER", "google")
    with pytest.raises(gs.GeocodingNotConfigured):
        gs.search_places("anything")
    with pytest.raises(gs.GeocodingNotConfigured):
        gs.reverse_geocode(1.0, 2.0)
    assert fake_http.calls == []


def test_unknown_provider_is_not_configured(monkeypatch):
    monkeypatch.setenv("GEOCODING_PROVIDER", "mapquestish")
    with pytest.raises(gs.GeocodingNotConfigured):
        gs.search_places("x")


# ------------------------------------------------------- failure handling

def test_network_error_message_never_leaks_url_or_key(fake_http, monkeypatch):
    monkeypatch.setenv("GEOCODING_PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "SECRET-KEY-123")

    def boom(*a, **k):
        raise requests.ConnectionError("failed: https://maps.googleapis.com/...&key=SECRET-KEY-123")

    fake_http.state["respond"] = boom
    with pytest.raises(gs.GeocodingError) as ei:
        gs.reverse_geocode(1.0, 2.0)
    assert "SECRET-KEY-123" not in str(ei.value)
    assert "googleapis" not in str(ei.value)


@pytest.mark.parametrize("resp", [
    FakeResponse({}, status_code=500),
    FakeResponse({}, status_code=429),
    FakeResponse(bad_json=True),
    FakeResponse({"unexpected": "shape"}),  # dict where Nominatim search returns a list
])
def test_upstream_failures_raise_geocoding_error(fake_http, resp):
    fake_http.state["respond"] = lambda *a, **k: resp
    with pytest.raises(gs.GeocodingError):
        gs.search_places("x")


def test_results_are_cached_and_errors_are_not(fake_http):
    fake_http.state["respond"] = lambda *a, **k: FakeResponse([NOMINATIM_ITEM])
    gs.search_places("Charminar ")
    gs.search_places("charminar")
    assert len(fake_http.calls) == 1  # normalized query hits the cache

    gs.clear_cache()
    fake_http.state["respond"] = lambda *a, **k: FakeResponse({}, status_code=500)
    for _ in range(2):
        with pytest.raises(gs.GeocodingError):
            gs.search_places("y")
    assert len(fake_http.calls) == 3  # both failed attempts went upstream


def test_long_provider_values_are_clipped_to_column_widths(fake_http):
    item = {**NOMINATIM_ITEM, "name": "N" * 400, "display_name": "A" * 900}
    fake_http.state["respond"] = lambda *a, **k: FakeResponse([item])
    p = gs.search_places("long")[0]
    assert len(p.name) == 150 and len(p.address) == 500


# ------------------------------------------------------------------ routes

def test_places_search_route(client, make_user, fake_http):
    client.login(make_user("alice"))
    fake_http.state["respond"] = lambda *a, **k: FakeResponse([NOMINATIM_ITEM])

    r = client.get("/api/locations/places/search", params={"q": "charminar", "limit": 2})
    assert r.status_code == 200
    item = r.json()["items"][0]
    assert item["name"] == "Charminar" and item["place_id"] == "osm:N123456"
    assert item["provider"] == "nominatim"


def test_places_search_requires_login(client, fake_http):
    assert client.get("/api/locations/places/search", params={"q": "abc"}).status_code == 401
    assert client.get(
        "/api/locations/reverse-geocode", params={"latitude": 1, "longitude": 1}
    ).status_code == 401
    assert fake_http.calls == []


def test_places_search_validation(client, make_user, fake_http):
    client.login(make_user())
    assert client.get("/api/locations/places/search", params={"q": "a"}).status_code in (400, 422)
    assert client.get("/api/locations/places/search", params={"q": "  "}).status_code == 400
    assert client.get(
        "/api/locations/places/search", params={"q": "abc", "latitude": 10}
    ).status_code == 400
    assert client.get(
        "/api/locations/places/search", params={"q": "abc", "latitude": 91, "longitude": 0}
    ).status_code in (400, 422)
    assert client.get("/api/locations/places/search", params={"q": "abc", "limit": 50}).status_code in (400, 422)
    assert fake_http.calls == []


def test_places_search_provider_errors_map_to_503_and_502(client, make_user, fake_http, monkeypatch):
    client.login(make_user())

    monkeypatch.setenv("GEOCODING_PROVIDER", "google")  # no key
    assert client.get("/api/locations/places/search", params={"q": "abc"}).status_code == 503

    monkeypatch.setenv("GEOCODING_PROVIDER", "nominatim")
    fake_http.state["respond"] = lambda *a, **k: FakeResponse({}, status_code=500)
    assert client.get("/api/locations/places/search", params={"q": "abc"}).status_code == 502


def test_reverse_geocode_route(client, make_user, fake_http):
    client.login(make_user())
    fake_http.state["respond"] = lambda *a, **k: FakeResponse(NOMINATIM_ITEM)
    r = client.get("/api/locations/reverse-geocode", params={"latitude": 17.36, "longitude": 78.47})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Charminar" and body["city"] == "Hyderabad" and body["address"]


def test_reverse_geocode_not_found_and_bad_coordinates(client, make_user, fake_http):
    client.login(make_user())
    fake_http.state["respond"] = lambda *a, **k: FakeResponse({"error": "Unable to geocode"})
    assert client.get(
        "/api/locations/reverse-geocode", params={"latitude": 0, "longitude": 0}
    ).status_code == 404
    assert client.get(
        "/api/locations/reverse-geocode", params={"latitude": 100, "longitude": 0}
    ).status_code in (400, 422)
    assert client.get("/api/locations/reverse-geocode").status_code in (400, 422)


def test_reverse_geocode_does_not_persist_anything(client, make_user, fake_http, db):
    client.login(make_user())
    fake_http.state["respond"] = lambda *a, **k: FakeResponse(NOMINATIM_ITEM)
    client.get("/api/locations/reverse-geocode", params={"latitude": 17.36, "longitude": 78.47})
    assert db.query(models.Location).count() == 0


def test_static_place_routes_not_shadowed_by_location_id_route(client, make_user, fake_http):
    client.login(make_user())
    fake_http.state["respond"] = lambda *a, **k: FakeResponse([])
    assert client.get("/api/locations/places/search", params={"q": "abc"}).status_code == 200


# ---- picking a provider result and saving it (the full "search then attach" flow)

def test_provider_result_can_be_saved_and_is_deduped_by_place_id(client, make_user, fake_http, db):
    client.login(make_user())
    fake_http.state["respond"] = lambda *a, **k: FakeResponse([NOMINATIM_ITEM])
    place = client.get("/api/locations/places/search", params={"q": "charminar"}).json()["items"][0]

    first = client.post("/api/locations", json=place)
    second = client.post("/api/locations", json=place)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["place_id"] == "osm:N123456"
    assert db.query(models.Location).count() == 1
