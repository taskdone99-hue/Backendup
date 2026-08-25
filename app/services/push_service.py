"""
Push notification service.

Currently acts as a safe no-op if Firebase/FCM is not configured.
This allows the chat API to start normally.
"""

import logging

logger = logging.getLogger(__name__)


def send_push(
    device_tokens: list[str],
    title: str,
    body: str,
    data: dict | None = None,
) -> None:
    """
    Send a push notification.

    For now this safely skips sending if push notification
    configuration is not available.
    """

    if not device_tokens:
        return

    logger.info(
        "Push notification requested for %d device(s): %s",
        len(device_tokens),
        title,
    )

    # Firebase/FCM sending can be added here later.
    return