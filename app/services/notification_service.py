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

# Which preference field gates a given notification type — see
# models.NotificationPreference / GET+PUT /api/notifications/preferences.
# Types with no entry here (share, brand_collaboration_*, moderation_warning,
# other) always go through: there's no matching toggle for them, and a few
# of those (moderation_warning) shouldn't be opt-out-able at all.
_PREFERENCE_FIELD_BY_TYPE = {
    models.NotificationType.like: "likes_enabled",
    models.NotificationType.comment: "comments_enabled",
    models.NotificationType.follow: "follows_enabled",
    models.NotificationType.follow_request: "follows_enabled",
    models.NotificationType.mention: "mentions_enabled",
    # Being tagged is treated like being mentioned for opt-out purposes.
    models.NotificationType.tag: "mentions_enabled",
    models.NotificationType.tag_request: "mentions_enabled",
    models.NotificationType.message: "messages_enabled",
    models.NotificationType.collaboration_request: "collaboration_requests_enabled",
    models.NotificationType.collaboration_accepted: "collaboration_requests_enabled",
    models.NotificationType.collaboration_rejected: "collaboration_requests_enabled",
    models.NotificationType.collaboration_cancelled: "collaboration_requests_enabled",
}


async def notify_user(
    db: Session,
    *,
    user_id: int,
    actor: models.User,
    notif_type: models.NotificationType,
    message: str,
    target_type: str,
    target_id: int,
    push_body: str | None = None,
) -> models.Notification | None:
    """Create a notification for `user_id` and fan it out. No-op if the
    actor is notifying themselves (e.g. can't follow-request yourself).

    `message` is used for the DB row and the WS payload. `push_body`, if
    given, is used for the FCM push body instead (e.g. a message preview) —
    defaults to `message` when omitted."""
    if actor.id == user_id:
        return None

    prefs = (
        db.query(models.NotificationPreference)
        .filter(models.NotificationPreference.user_id == user_id)
        .first()
    )
    # No row yet = defaults = everything enabled (see
    # notification_routes._get_or_create_preferences, which only
    # materializes the row on first read/write of the endpoint).
    pref_field = _PREFERENCE_FIELD_BY_TYPE.get(notif_type)
    if prefs is not None and pref_field is not None and not getattr(prefs, pref_field):
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
    ] if (prefs is None or prefs.push_enabled) else []
    if tokens:
        try:
            await run_in_threadpool(
                send_push,
                tokens,
                title=actor.username,
                body=push_body or message,
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
