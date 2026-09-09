"""
SMS delivery for OTP codes.

This just logs the OTP to the console — good enough for local dev/demos
and for any remaining local-OTP-table phone flow (e.g. forgot-password
by phone), and requires no external account.

Real phone-OTP delivery + verification for login/signup now goes
through the MSG91 OTP Widget instead (see app.services.msg91_service):
the widget sends and verifies the OTP entirely client-side against
MSG91, then hands the backend a short-lived access-token to confirm
via POST /api/auth/verify-msg91-token. This module is no longer in
that path — it previously also supported Twilio Verify as a real-SMS
backend, which has been removed in favor of MSG91.
"""
import logging

logger = logging.getLogger("sms_service")
logging.basicConfig(level=logging.INFO)


def send_otp_sms(phone_number: str, otp: str) -> None:
    """Console/log backend for a locally generated OTP (e.g. the
    forgot-password-by-phone flow, which still uses the local OTP
    table rather than the MSG91 Widget)."""
    logger.info("[SMS to %s] Your verification code is %s", phone_number, otp)
