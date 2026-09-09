"""
MSG91 OTP Widget — server-side access-token verification.

Replaces Twilio Verify for phone OTP. With the MSG91 Widget, OTP
send/retry/verify all happen client-side against MSG91 directly:

    1. Frontend: enter mobile number
    2. Frontend: send OTP (widget -> MSG91)
    3. Frontend: enter OTP
    4. Frontend: widget verifies the OTP with MSG91
    5. Frontend: receives a short-lived MSG91 access-token

The frontend then hands that access-token to us. This backend is the
only party that ever holds MSG91_AUTHKEY, and the access-token by
itself proves nothing to this API until *we* confirm it with MSG91's
verifyAccessToken endpoint — the frontend cannot forge a verified user
by fabricating a token, since we always re-check it server-side.

MSG91_AUTHKEY is read from the environment only. It is never sent to,
logged for, or accepted from the frontend.
"""
import logging
import os
from typing import Optional

import requests

logger = logging.getLogger("msg91_service")
logging.basicConfig(level=logging.INFO)

MSG91_AUTHKEY = os.getenv("MSG91_AUTHKEY")
MSG91_VERIFY_URL = "https://control.msg91.com/api/v5/widget/verifyAccessToken"
# Keep short — this sits in the middle of an interactive login/signup request.
MSG91_REQUEST_TIMEOUT_SECONDS = int(os.getenv("MSG91_REQUEST_TIMEOUT_SECONDS", "10"))


def is_msg91_configured() -> bool:
    return bool(MSG91_AUTHKEY)


class MSG91ConfigError(Exception):
    """MSG91_AUTHKEY isn't set, so no verification can be attempted."""


class MSG91VerificationError(Exception):
    """MSG91 was reached and responded, but rejected the access-token
    (invalid, expired, already used, or malformed)."""

    def __init__(self, message: str, raw_response: Optional[dict] = None):
        super().__init__(message)
        self.raw_response = raw_response


class MSG91APIError(Exception):
    """MSG91 could not be reached, timed out, returned a server error,
    or returned a response we can't parse/trust."""


def _extract_identifier(payload: dict) -> Optional[str]:
    """
    A successful verifyAccessToken response has the shape
    {"type": "success", "message": <verified contact>}. In practice
    `message` is either the verified mobile number / email as a plain
    string, or (depending on widget/account configuration) a small
    object carrying it under a `mobile`/`identifier`/`email`/`contact`
    key. This normalizes either shape to a bare identifier string.
    """
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    if isinstance(message, dict):
        for key in ("mobile", "identifier", "email", "contact"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def verify_msg91_access_token(access_token: str) -> str:
    """
    Verifies `access_token` (issued client-side by the MSG91 OTP Widget)
    against MSG91's verifyAccessToken API and returns the verified
    identifier (phone number or email, exactly as MSG91 reports it —
    the caller is responsible for normalizing it) on success.

    Raises:
        MSG91ConfigError: MSG91_AUTHKEY isn't configured.
        MSG91VerificationError: MSG91 rejected the token.
        MSG91APIError: MSG91 was unreachable/timed out/errored, or its
            response couldn't be parsed or trusted.
    """
    if not is_msg91_configured():
        raise MSG91ConfigError("MSG91_AUTHKEY is not configured")

    if not access_token or not access_token.strip():
        raise MSG91VerificationError("Access token is required")

    try:
        response = requests.post(
            MSG91_VERIFY_URL,
            json={"authkey": MSG91_AUTHKEY, "access-token": access_token},
            timeout=MSG91_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        logger.error("MSG91 verifyAccessToken request failed: %s", e)
        raise MSG91APIError("Could not reach MSG91 to verify the access token") from e

    try:
        payload = response.json()
    except ValueError as e:
        logger.error(
            "MSG91 verifyAccessToken returned a non-JSON body (status=%s)",
            response.status_code,
        )
        raise MSG91APIError("MSG91 returned an unexpected response") from e

    if response.status_code >= 500:
        logger.error(
            "MSG91 verifyAccessToken server error: status=%s body=%s",
            response.status_code, payload,
        )
        raise MSG91APIError("MSG91 is currently unavailable")

    if not isinstance(payload, dict) or payload.get("type") != "success":
        message = payload.get("message") if isinstance(payload, dict) else None
        logger.info("MSG91 verifyAccessToken rejected token: %s", message or payload)
        raise MSG91VerificationError(
            message or "Invalid or expired access token",
            raw_response=payload if isinstance(payload, dict) else None,
        )

    identifier = _extract_identifier(payload)
    if not identifier:
        logger.error(
            "MSG91 verifyAccessToken success response had no extractable identifier: %s",
            payload,
        )
        raise MSG91APIError("MSG91 verification succeeded but returned no identifier")

    logger.info("MSG91 access token verified (identifier ends in %s)", identifier[-4:])
    return identifier
