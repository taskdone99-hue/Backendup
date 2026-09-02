"""
Central fan-out for a single notification, used by every place in the app
that notifies a user (follow, follow request, follow-request accepted,
story reaction/reply, etc.):

  1. Persists the `Notification` row — this is what GET /api/notifications
     reads from, unchanged from before.
  2. Pushes it instantly over the notifications WebSocket
     (app/routers/notification_routes.py) if the user has one open — this
     is the "appears without an app reload" path.
  3. Sends an FCM push to every device token registered to the user — this
     is what fires when the app is backgrounded or fully closed, so the
     WebSocket alone wouldn't reach it.

Steps 2 and 3 are both best-effort: a failure there is logged and swallowed
rather than raised, since the DB write in step 1 is the one thing that must
succeed (it's the source of truth GET /api/notifications relies on).
"""

import logging

from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import models, schemas
from app.services.push_service import send_push
from app.ws_manager import notification_manager

logger = logging.getLogger(__name__)


async def notify_user(
    db: Session,
    *,
    user_id: int,
    actor: models.User,
    notif_type: models.NotificationType,
    message: str,
    target_type: str,
    target_id: int,
) -> models.Notification | None:
    """Create a notification for `user_id` and fan it out. No-op if the
    actor is notifying themselves (e.g. can't follow-request yourself)."""
    if actor.id == user_id:
        return None

    notification = models.Notification(
        user_id=user_id,
        actor_id=actor.id,
        type=notif_type,
        message=message,
        target_type=target_type,
        target_id=target_id,
    )
    db.add(notification)
    db.commit()
    db.refresh(notification)

    # Same shape as a GET /api/notifications item (schemas.NotificationOut),
    # just wrapped with a "type" envelope so the client can dispatch on it
    # alongside whatever else might arrive on this socket.
    payload = {
        "type": "notification",
        "notification": schemas.NotificationOut.model_validate(notification).model_dump(mode="json"),
    }
    try:
        await notification_manager.send_to_user(user_id, payload)
    except Exception:
        logger.exception("Failed to push notification %s over WebSocket", notification.id)

    tokens = [
        row[0]
        for row in db.query(models.DeviceToken.token)
        .filter(models.DeviceToken.user_id == user_id)
        .all()
    ]
    if tokens:
        try:
            await run_in_threadpool(
                send_push,
                tokens,
                title=actor.username,
                body=message,
                data={
                    "type": notification.type.value,
                    "notification_id": str(notification.id),
                    "target_type": target_type or "",
                    "target_id": str(target_id) if target_id is not None else "",
                },
            )
        except Exception:
            logger.exception("Failed to send FCM push for notification %s", notification.id)

    return notification
