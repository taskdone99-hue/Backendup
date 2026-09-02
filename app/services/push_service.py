"""
Push notification service — Firebase Cloud Messaging (FCM).

The Firebase Admin SDK is initialized lazily, on first send, from
whichever of these is set (checked in this order):
  - FIREBASE_CREDENTIALS_JSON        the service-account JSON itself, as a string
  - GOOGLE_APPLICATION_CREDENTIALS   path to a service-account JSON file
  - FIREBASE_SERVICE_ACCOUNT_FILE    same as above, an explicit alternative name

If none are set (or firebase-admin isn't installed), this stays a safe
no-op — same behavior as before this was implemented — so the API still
starts and runs normally without FCM configured.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

_firebase_app = None
_init_attempted = False


def _get_firebase_app():
    """Returns the initialized Firebase app, or None if FCM isn't configured.
    Only ever attempts initialization once per process."""
    global _firebase_app, _init_attempted
    if _init_attempted:
        return _firebase_app
    _init_attempted = True

    try:
        import firebase_admin
        from firebase_admin import credentials
    except ImportError:
        logger.info("firebase-admin not installed — push notifications disabled")
        return None

    creds_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
    creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv(
        "FIREBASE_SERVICE_ACCOUNT_FILE"
    )

    cred = None
    try:
        if creds_json:
            cred = credentials.Certificate(json.loads(creds_json))
        elif creds_path and os.path.exists(creds_path):
            cred = credentials.Certificate(creds_path)
    except Exception:
        logger.exception("Failed to load Firebase credentials — push notifications disabled")
        return None

    if cred is None:
        logger.info("No Firebase credentials configured — push notifications disabled")
        return None

    try:
        _firebase_app = firebase_admin.initialize_app(cred)
    except ValueError:
        # Already initialized in this process (e.g. re-imported under a
        # test runner) — reuse the existing app instead of erroring.
        _firebase_app = firebase_admin.get_app()

    return _firebase_app


def send_push(
    device_tokens: list[str],
    title: str,
    body: str,
    data: dict | None = None,
) -> None:
    """
    Send an FCM push to one or more device tokens.

    Best-effort and synchronous (safe to call from a threadpool — see
    notification_service.notify_user, which is the one place that calls
    this): any failure is logged and swallowed rather than raised, since
    the notification has already been persisted to the DB regardless of
    whether the push itself succeeds.
    """
    if not device_tokens:
        return

    app = _get_firebase_app()
    if app is None:
        logger.info(
            "Push notification skipped (FCM not configured) for %d device(s): %s",
            len(device_tokens),
            title,
        )
        return

    from firebase_admin import messaging

    # FCM data payloads must be string -> string.
    string_data = {str(k): str(v) for k, v in (data or {}).items()}

    messages = [
        messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data=string_data,
            token=token,
        )
        for token in device_tokens
    ]

    try:
        response = messaging.send_each(messages, app=app)
    except Exception:
        logger.exception("FCM send failed for %d device(s)", len(device_tokens))
        return

    failures = [
        (token, result.exception)
        for token, result in zip(device_tokens, response.responses)
        if not result.success
    ]
    if failures:
        logger.warning(
            "FCM push failed for %d/%d device(s): %s",
            len(failures),
            len(device_tokens),
            [f"{tok[:12]}...: {exc}" for tok, exc in failures],
        )
