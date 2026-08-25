"""
Discord server stats.

Uses Discord's public server-widget JSON endpoint
(https://discord.com/api/guilds/{id}/widget.json), which requires no bot
token or auth — just the guild id, and the guild's "Server Widget" setting
turned on. Set DISCORD_GUILD_ID to enable live stats; leave it unset to get
a stub response instead (no network call).
"""

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger("discord_service")
logging.basicConfig(level=logging.INFO)

DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")
DISCORD_INVITE_URL = os.getenv("DISCORD_INVITE_URL")

_WIDGET_URL = "https://discord.com/api/guilds/{guild_id}/widget.json"


def get_server_stats() -> dict:
    """Returns a dict matching schemas.DiscordServerStatsOut fields."""
    if not DISCORD_GUILD_ID:
        return {
            "guild_id": None,
            "name": None,
            "member_count": None,
            "online_count": None,
            "invite_url": DISCORD_INVITE_URL,
            "live": False,
        }

    try:
        request = urllib.request.Request(
            _WIDGET_URL.format(guild_id=DISCORD_GUILD_ID),
            headers={"User-Agent": "backend-api/1.0"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))

        members = data.get("members", [])
        return {
            "guild_id": DISCORD_GUILD_ID,
            "name": data.get("name"),
            "member_count": len(members) or None,
            "online_count": data.get("presence_count", len(members)),
            "invite_url": data.get("instant_invite") or DISCORD_INVITE_URL,
            "live": True,
        }
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        # Widget disabled, guild id wrong, or network unavailable — fall
        # back to a stub rather than failing the whole request.
        logger.info("[Discord] widget fetch failed for guild %s: %s", DISCORD_GUILD_ID, exc)
        return {
            "guild_id": DISCORD_GUILD_ID,
            "name": None,
            "member_count": None,
            "online_count": None,
            "invite_url": DISCORD_INVITE_URL,
            "live": False,
        }
