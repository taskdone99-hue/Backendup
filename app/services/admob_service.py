"""
Google AdMob configuration for the mobile clients.

Ads are rendered client-side by the AdMob SDK. The backend's only jobs are
(1) hand the app its AdMob app/ad-unit IDs per placement and platform, so
they can change without shipping a new build, and (2) record impression /
click analytics (see ads_routes.py). It never calculates or stores AdMob
revenue — AdMob's own reporting is the source of truth for that.

Everything is read from the environment on each call (not at import time),
so changing .env + restarting is the only step needed and tests can flip
values with monkeypatch.
"""

import os

PLATFORMS = ("android", "ios")

# placement -> (env-var suffix, AdMob ad format)
PLACEMENTS = {
    "feed": ("FEED_NATIVE", "native"),
    "reels": ("REELS_NATIVE", "native"),
    "stories": ("STORIES_NATIVE", "native"),
    "banner": ("BANNER", "banner"),
    "interstitial": ("INTERSTITIAL", "interstitial"),
    "app_open": ("APP_OPEN", "app_open"),
}

_TRUTHY = {"1", "true", "yes", "on"}


def _env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def is_admob_enabled() -> bool:
    # Off unless explicitly enabled: a missing variable must never turn ads on.
    return (_env("ADMOB_ENABLED") or "").lower() in _TRUTHY


def build_config(platform: str) -> dict:
    """Placement-based AdMob config for one platform ("android" or "ios").

    When ADMOB_ENABLED is off, nothing is exposed (no IDs at all). When it is
    on, a placement is only enabled if its ad-unit ID is actually configured,
    so the app never tries to load an ad from an empty unit ID.
    """
    if platform not in PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform}")

    enabled = is_admob_enabled()
    prefix = f"ADMOB_{platform.upper()}"

    placements = {}
    for name, (suffix, ad_format) in PLACEMENTS.items():
        ad_unit_id = _env(f"{prefix}_{suffix}_ID") if enabled else None
        placements[name] = {
            "enabled": ad_unit_id is not None,
            "ad_format": ad_format,
            "ad_unit_id": ad_unit_id,
        }

    return {
        "enabled": enabled,
        "platform": platform,
        "app_id": _env(f"{prefix}_APP_ID") if enabled else None,
        "placements": placements,
    }
