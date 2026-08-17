"""
SMS delivery for OTP codes.

By default this just logs the OTP to the console, so the API works out of
the box with no external account needed for local dev / demos. Set the
TWILIO_* env vars to switch to real SMS delivery via Twilio.

To use a different provider (MSG91, AWS SNS, Fast2SMS, etc.), just swap the
body of `send_otp_sms` for that provider's send call — the rest of the app
doesn't need to change.
"""

import logging
import os

logger = logging.getLogger("sms_service")
logging.basicConfig(level=logging.INFO)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")


def send_otp_sms(phone_number: str, otp: str) -> None:
    message = f"Your verification code is {otp}. It expires in a few minutes."

    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER:
        _send_via_twilio(phone_number, message)
    else:
        # Console/log backend — good enough for local dev and demos.
        logger.info("[SMS to %s] %s", phone_number, message)


def _send_via_twilio(phone_number: str, message: str) -> None:
    # Uncomment after `pip install twilio` and setting the TWILIO_* env vars.
    #
    from twilio.rest import Client
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    client.messages.create(
        body=message,
        from_=TWILIO_FROM_NUMBER,
        to=phone_number,
    )
    logger.info("[Twilio SMS to %s] %s", phone_number, message)
