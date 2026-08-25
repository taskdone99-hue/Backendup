"""
SMS delivery for OTP codes.

By default this just logs the OTP to the console, so the API works out of
the box with no external account needed for local dev / demos. Set
TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_VERIFY_SERVICE_SID to
switch to real SMS delivery via Twilio Verify.

This uses Twilio Verify (not plain SMS) because /api/auth/verify-otp checks
phone OTPs against Twilio Verify's own verification_checks API — Verify
generates and validates its own code independently of the app's local
OTP table, so the two sides need to agree on which system is authoritative.
Verify is enabled if and only if TWILIO_VERIFY_SERVICE_SID is configured;
see auth_routes.verify_otp for the matching check-side logic.

is_twilio_configured() lets callers (verify_otp) know which path is live.
"""
import os
import logging

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

logger = logging.getLogger("sms_service")
logging.basicConfig(level=logging.INFO)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_VERIFY_SERVICE_SID = os.getenv("TWILIO_VERIFY_SERVICE_SID")


def is_twilio_configured() -> bool:
    return bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_VERIFY_SERVICE_SID)


def send_otp_sms(phone_number: str, otp: str) -> None:
    """
    `otp` is the app's own locally generated code — used only for the
    console-log fallback. Once Twilio Verify is configured, Twilio
    generates and sends its own code instead, and `otp` is ignored for
    delivery (it's still stored locally, but verify_otp checks phone OTPs
    against Twilio, not the local table — see is_twilio_configured()).
    """
    if not is_twilio_configured():
        # Console/log backend — good enough for local dev and demos, and
        # keeps signup/login working end-to-end before Twilio is set up.
        logger.info("[SMS to %s] Your verification code is %s", phone_number, otp)
        return

    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        verification = (
            client.verify.v2
            .services(TWILIO_VERIFY_SERVICE_SID)
            .verifications
            .create(channel="sms", to=phone_number)
        )
        logger.info(
            "Twilio Verify SMS sent to %s, status=%s", phone_number, verification.status
        )
    except TwilioRestException as e:
        # Don't 500 the whole signup/login flow over an SMS provider hiccup
        # (bad number formatting, unverified number on a trial account,
        # account balance, etc). The caller can offer a resend.
        logger.error("Twilio Verify send failed for %s: %s", phone_number, e)
