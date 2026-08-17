"""
SMS delivery for OTP codes.

By default this just logs the OTP to the console, so the API works out of
the box with no external account needed for local dev / demos. Set the
TWILIO_* env vars to switch to real SMS delivery via Twilio.

To use a different provider (MSG91, AWS SNS, Fast2SMS, etc.), just swap the
body of `send_otp_sms` for that provider's send call — the rest of the app
doesn't need to change.
"""
import os
import logging
from twilio.rest import Client

logger = logging.getLogger(__name__)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_VERIFY_SERVICE_SID = os.getenv("TWILIO_VERIFY_SERVICE_SID")


def send_otp_sms(phone_number: str, otp: str) -> None:
    client = Client(
        TWILIO_ACCOUNT_SID,
        TWILIO_AUTH_TOKEN,
    )

    verification = (
        client.verify.v2
        .services(TWILIO_VERIFY_SERVICE_SID)
        .verifications
        .create(
            channel="sms",
            to=phone_number,
        )
    )

    logger.info(
        "Twilio Verify SMS sent to %s, status=%s",
        phone_number,
        verification.status,
    )