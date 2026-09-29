"""
Google AdMob backend support: GET /api/ads/config, POST /api/ads/impression,
POST /api/ads/click.

Config is read from ADMOB_* environment variables on every request, so these
tests just monkeypatch the environment. Every ADMOB_* variable is cleared
first so results never depend on the developer's real .env.
"""

import os

import pytest

from app import models
from app.auth import get_current_user
from app.main import app

PLACEMENTS = ["feed", "reels", "stories", "banner", "interstitial", "app_open"]

# placement -> env suffix used in ADMOB_<ANDROID|IOS>_<suffix>_ID
SUFFIX = {
    "feed": "FEED_NATIVE",
    "reels": "REELS_NATIVE",
    "stories": "STORIES_NATIVE",
    "banner": "BANNER",
    "interstitial": "INTERSTITIAL",
    "app_open": "APP_OPEN",
}
FORMAT = {
    "feed": "native",
    "reels": "native",
    "stories": "native",
    "banner": "banner",
    "interstitial": "interstitial",
    "app_open": "app_open",
}


def unit_id(platform: str, placement: str) -> str:
    return f"ca-app-pub-1111111111111111/{platform}-{placement}"


def app_id(platform: str) -> str:
    return f"ca-app-pub-1111111111111111~{platform}-app"


@pytest.fixture(autouse=True)
def clean_admob_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("ADMOB_")]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def full_admob_env(monkeypatch):
    """Every ID for both platforms, ADMOB_ENABLED=true."""
    monkeypatch.setenv("ADMOB_ENABLED", "true")
    for platform in ("android", "ios"):
        monkeypatch.setenv(f"ADMOB_{platform.upper()}_APP_ID", app_id(platform))
        for placement in PLACEMENTS:
            monkeypatch.setenv(
                f"ADMOB_{platform.upper()}_{SUFFIX[placement]}_ID", unit_id(platform, placement)
            )


@pytest.fixture()
def user(make_user):
    return make_user("adviewer")


@pytest.fixture()
def authed(client, user):
    client.login(user)
    return client


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


def test_all_three_endpoints_reject_unauthenticated(client, db):
    assert client.get("/api/ads/config", params={"platform": "android"}).status_code == 401
    body = {"placement": "feed"}
    assert client.post("/api/ads/impression", json=body).status_code == 401
    assert client.post("/api/ads/click", json=body).status_code == 401
    assert db.query(models.AdImpression).count() == 0
    assert db.query(models.AdClick).count() == 0


def test_real_auth_dependency_rejects_missing_and_bad_token(client, db):
    """The other tests stub get_current_user; drop the stub to prove the real
    JWT dependency is what guards these routes."""
    app.dependency_overrides.pop(get_current_user, None)
    bad = {"Authorization": "Bearer not-a-real-token"}
    body = {"placement": "feed"}

    for call in (
        lambda h: client.get("/api/ads/config", params={"platform": "ios"}, headers=h),
        lambda h: client.post("/api/ads/impression", json=body, headers=h),
        lambda h: client.post("/api/ads/click", json=body, headers=h),
    ):
        # No header: 401 or 403 depending on the installed FastAPI's HTTPBearer.
        assert call({}).status_code in (401, 403)
        assert call(bad).status_code == 401
    assert db.query(models.AdImpression).count() == 0
    assert db.query(models.AdClick).count() == 0


def test_authenticated_can_use_all_three(authed, full_admob_env):
    assert authed.get("/api/ads/config", params={"platform": "android"}).status_code == 200
    assert authed.post("/api/ads/impression", json={"placement": "feed"}).status_code == 201
    assert authed.post("/api/ads/click", json={"placement": "feed"}).status_code == 201


# --------------------------------------------------------------------------
# Placement validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", ["impression", "click"])
@pytest.mark.parametrize("placement", PLACEMENTS)
def test_valid_placements_are_accepted(authed, db, endpoint, placement):
    r = authed.post(f"/api/ads/{endpoint}", json={"placement": placement})
    assert r.status_code == 201, r.text
    assert r.json()["placement"] == placement

    model = models.AdImpression if endpoint == "impression" else models.AdClick
    rows = db.query(model).all()
    assert len(rows) == 1
    assert rows[0].placement == placement


@pytest.mark.parametrize("endpoint", ["impression", "click"])
@pytest.mark.parametrize(
    "bad_placement",
    ["explore", "sidebar", "", "   ", "feed;drop", "native", None, 5],
)
def test_invalid_placement_returns_400(authed, db, endpoint, bad_placement):
    r = authed.post(f"/api/ads/{endpoint}", json={"placement": bad_placement})
    assert r.status_code == 400
    assert "message" in r.json()
    assert db.query(models.AdImpression).count() == 0
    assert db.query(models.AdClick).count() == 0


@pytest.mark.parametrize("endpoint", ["impression", "click"])
def test_missing_placement_returns_400(authed, endpoint):
    # The old contract only required ad_id; placement is now mandatory.
    assert authed.post(f"/api/ads/{endpoint}", json={}).status_code == 400
    assert authed.post(f"/api/ads/{endpoint}", json={"ad_id": "legacy-ad"}).status_code == 400


def test_invalid_placement_message_lists_valid_ones(authed):
    r = authed.post("/api/ads/impression", json={"placement": "explore"})
    assert r.status_code == 400
    for placement in PLACEMENTS:
        assert placement in r.json()["message"]


def test_placement_is_normalised(authed, db):
    r = authed.post("/api/ads/impression", json={"placement": "  FEED "})
    assert r.status_code == 201
    assert r.json()["placement"] == "feed"
    assert db.query(models.AdImpression).one().placement == "feed"


@pytest.mark.parametrize("endpoint", ["impression", "click"])
def test_invalid_platform_returns_400(authed, endpoint):
    r = authed.post(f"/api/ads/{endpoint}", json={"placement": "feed", "platform": "windows"})
    assert r.status_code == 400


# --------------------------------------------------------------------------
# Analytics storage
# --------------------------------------------------------------------------


def test_impression_is_stored_with_user_and_metadata(authed, db, user):
    r = authed.post(
        "/api/ads/impression",
        json={
            "placement": "reels",
            "platform": "IOS",
            "ad_unit_id": unit_id("ios", "reels"),
            "ad_id": "resp-123",
        },
    )
    assert r.status_code == 201
    assert r.json() == {
        "message": "Impression recorded",
        "placement": "reels",
        "platform": "ios",
        "ad_unit_id": unit_id("ios", "reels"),
        "ad_id": "resp-123",
    }
    row = db.query(models.AdImpression).one()
    assert (row.user_id, row.placement, row.platform, row.ad_unit_id, row.ad_id) == (
        user.id,
        "reels",
        "ios",
        unit_id("ios", "reels"),
        "resp-123",
    )
    assert row.created_at is not None
    assert db.query(models.AdClick).count() == 0


def test_click_is_stored_separately_from_impressions(authed, db, user):
    r = authed.post("/api/ads/click", json={"placement": "banner", "platform": "android"})
    assert r.status_code == 201
    assert r.json()["message"] == "Click recorded"
    row = db.query(models.AdClick).one()
    assert (row.user_id, row.placement, row.platform) == (user.id, "banner", "android")
    assert row.ad_id is None and row.ad_unit_id is None  # optional fields
    assert db.query(models.AdImpression).count() == 0


def test_repeated_events_are_each_recorded(authed, db):
    for _ in range(3):
        assert authed.post("/api/ads/impression", json={"placement": "feed"}).status_code == 201
    assert authed.post("/api/ads/click", json={"placement": "feed"}).status_code == 201
    assert db.query(models.AdImpression).count() == 3
    assert db.query(models.AdClick).count() == 1


def test_events_store_no_revenue_data(authed):
    """Analytics only: no revenue/earnings column exists and none is echoed back."""
    forbidden = ("revenue", "earning", "cpm", "micros", "value", "price", "amount")
    for model in (models.AdImpression, models.AdClick):
        cols = [c.name for c in model.__table__.columns]
        assert not [c for c in cols if any(f in c for f in forbidden)], cols

    r = authed.post("/api/ads/impression", json={"placement": "feed", "revenue": 9.99})
    assert r.status_code == 201
    assert not [k for k in r.json() if any(f in k for f in forbidden)]


# --------------------------------------------------------------------------
# GET /api/ads/config
# --------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["android", "ios"])
def test_config_returns_platform_ids_per_placement(authed, full_admob_env, platform):
    other = "ios" if platform == "android" else "android"
    r = authed.get("/api/ads/config", params={"platform": platform})
    assert r.status_code == 200
    data = r.json()

    assert data["enabled"] is True
    assert data["platform"] == platform
    assert data["app_id"] == app_id(platform)
    assert set(data["placements"]) == set(PLACEMENTS)
    for placement in PLACEMENTS:
        assert data["placements"][placement] == {
            "enabled": True,
            "ad_format": FORMAT[placement],
            "ad_unit_id": unit_id(platform, placement),
        }
    # The other platform's IDs must never leak into this response.
    assert other not in r.text


def test_config_android_and_ios_differ(authed, full_admob_env):
    a = authed.get("/api/ads/config", params={"platform": "android"}).json()
    i = authed.get("/api/ads/config", params={"platform": "ios"}).json()
    for placement in PLACEMENTS:
        assert a["placements"][placement]["ad_unit_id"] != i["placements"][placement]["ad_unit_id"]


@pytest.mark.parametrize("bad", ["windows", "", "ANDROID2"])
def test_config_invalid_platform_returns_400(authed, full_admob_env, bad):
    assert authed.get("/api/ads/config", params={"platform": bad}).status_code == 400


def test_config_requires_platform(authed, full_admob_env):
    assert authed.get("/api/ads/config").status_code == 400


@pytest.mark.parametrize("platform", ["android", "ios"])
def test_config_when_admob_disabled(authed, full_admob_env, monkeypatch, platform):
    monkeypatch.setenv("ADMOB_ENABLED", "false")
    r = authed.get("/api/ads/config", params={"platform": platform})
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is False
    assert data["app_id"] is None
    for placement in PLACEMENTS:
        assert data["placements"][placement]["enabled"] is False
        assert data["placements"][placement]["ad_unit_id"] is None
    # IDs are configured but must not be exposed while ads are off.
    assert "ca-app-pub" not in r.text


def test_config_disabled_when_env_var_unset(authed):
    data = authed.get("/api/ads/config", params={"platform": "android"}).json()
    assert data["enabled"] is False
    assert all(not p["enabled"] for p in data["placements"].values())


@pytest.mark.parametrize("value", ["TRUE", "True", "1", "yes", " on "])
def test_enabled_accepts_common_truthy_values(authed, monkeypatch, value):
    monkeypatch.setenv("ADMOB_ENABLED", value)
    assert authed.get("/api/ads/config", params={"platform": "ios"}).json()["enabled"] is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "", "garbage"])
def test_enabled_rejects_falsy_or_unknown_values(authed, monkeypatch, value):
    monkeypatch.setenv("ADMOB_ENABLED", value)
    assert authed.get("/api/ads/config", params={"platform": "ios"}).json()["enabled"] is False


@pytest.mark.parametrize("platform", ["android", "ios"])
def test_config_with_all_ad_unit_ids_missing(authed, monkeypatch, platform):
    monkeypatch.setenv("ADMOB_ENABLED", "true")
    r = authed.get("/api/ads/config", params={"platform": platform})
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is True
    assert data["app_id"] is None
    assert set(data["placements"]) == set(PLACEMENTS)
    for placement in PLACEMENTS:
        assert data["placements"][placement]["enabled"] is False
        assert data["placements"][placement]["ad_unit_id"] is None


def test_config_with_some_ad_unit_ids_missing(authed, monkeypatch):
    monkeypatch.setenv("ADMOB_ENABLED", "true")
    monkeypatch.setenv("ADMOB_ANDROID_APP_ID", app_id("android"))
    monkeypatch.setenv("ADMOB_ANDROID_FEED_NATIVE_ID", unit_id("android", "feed"))
    monkeypatch.setenv("ADMOB_ANDROID_BANNER_ID", unit_id("android", "banner"))
    monkeypatch.setenv("ADMOB_ANDROID_REELS_NATIVE_ID", "   ")  # blank counts as missing

    data = authed.get("/api/ads/config", params={"platform": "android"}).json()
    enabled = {name for name, p in data["placements"].items() if p["enabled"]}
    assert enabled == {"feed", "banner"}
    assert data["placements"]["feed"]["ad_unit_id"] == unit_id("android", "feed")
    for name in set(PLACEMENTS) - enabled:
        assert data["placements"][name]["ad_unit_id"] is None

    # iOS has nothing configured, independent of Android.
    ios = authed.get("/api/ads/config", params={"platform": "ios"}).json()
    assert all(not p["enabled"] for p in ios["placements"].values())
    assert ios["app_id"] is None


def test_config_ids_are_trimmed(authed, monkeypatch):
    monkeypatch.setenv("ADMOB_ENABLED", "true")
    monkeypatch.setenv("ADMOB_IOS_APP_OPEN_ID", "  ca-app-pub-1~open  ")
    p = authed.get("/api/ads/config", params={"platform": "ios"}).json()["placements"]["app_open"]
    assert p["ad_unit_id"] == "ca-app-pub-1~open"


def test_config_reflects_env_changes_without_restart(authed, monkeypatch):
    monkeypatch.setenv("ADMOB_ENABLED", "true")
    monkeypatch.setenv("ADMOB_IOS_BANNER_ID", "first")
    url = ("/api/ads/config", {"platform": "ios"})
    assert authed.get(url[0], params=url[1]).json()["placements"]["banner"]["ad_unit_id"] == "first"
    monkeypatch.setenv("ADMOB_IOS_BANNER_ID", "second")
    assert authed.get(url[0], params=url[1]).json()["placements"]["banner"]["ad_unit_id"] == "second"


# --------------------------------------------------------------------------
# Old custom-ads behaviour is gone
# --------------------------------------------------------------------------


def test_legacy_ad_config_fields_are_gone(authed, full_admob_env):
    data = authed.get("/api/ads/config", params={"platform": "android"}).json()
    for legacy in ("ad_network", "test_mode", "slots"):
        assert legacy not in data
    assert "explore" not in data["placements"]


def test_no_duplicate_ads_routes():
    paths = sorted(
        (route.path, tuple(sorted(route.methods)))
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/ads")
    )
    assert paths == [
        ("/api/ads/click", ("POST",)),
        ("/api/ads/config", ("GET",)),
        ("/api/ads/impression", ("POST",)),
    ]
