"""
Serves the MSG91 OTP Widget's browser-side configuration (widget id +
the widget's own token-auth) from environment variables, so the static
frontend never hardcodes them in source.

This is NOT the MSG91 server Authkey (MSG91_AUTHKEY) — that stays
backend-only and is used exclusively by app.services.msg91_service for
server-to-server verifyAccessToken calls. The widget id/token-auth
returned here are what MSG91's own client-side widget snippet expects
to receive in the browser (see MSG91's docs — every MSG91 web
integration example embeds these directly in page JS); this endpoint
only avoids hardcoding them as literals in committed source files.
"""
import os

from fastapi import APIRouter

router = APIRouter(prefix="/api/config", tags=["config"])

MSG91_WIDGET_ID = os.getenv("MSG91_WIDGET_ID", "")
MSG91_WIDGET_TOKEN = os.getenv("MSG91_WIDGET_TOKEN", "")


@router.get("/msg91-widget")
def get_msg91_widget_config():
    return {
        "widget_id": MSG91_WIDGET_ID,
        "widget_token": MSG91_WIDGET_TOKEN,
    }
