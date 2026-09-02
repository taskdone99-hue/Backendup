from fastapi import (
    APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status,
)
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app import models, schemas
from app.auth import get_current_user, get_user_from_raw_token
from app.ws_manager import notification_manager

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("", response_model=schemas.PaginatedNotificationsResponse)
def get_notifications(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    query = db.query(models.Notification).filter(models.Notification.user_id == current_user.id)
    total = query.count()
    unread_count = query.filter(models.Notification.is_read.is_(False)).count()
    items = (
        query.order_by(models.Notification.created_at.desc()).offset(offset).limit(limit).all()
    )
    return schemas.PaginatedNotificationsResponse(
        total=total, unread_count=unread_count, limit=limit, offset=offset, items=items
    )


@router.put("/{notification_id}/read", response_model=schemas.NotificationReadResponse)
def mark_notification_read(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    notification = (
        db.query(models.Notification)
        .filter(
            models.Notification.id == notification_id,
            models.Notification.user_id == current_user.id,
        )
        .first()
    )
    if notification is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")

    if not notification.is_read:
        notification.is_read = True
        db.commit()
        db.refresh(notification)

    return schemas.NotificationReadResponse(message="Notification marked as read", notification=notification)


@router.delete("/{notification_id}", response_model=schemas.MessageResponse)
def delete_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    notification = (
        db.query(models.Notification)
        .filter(models.Notification.id == notification_id)
        .first()
    )
    if notification is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found"
        )
    if notification.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only delete your own notifications",
        )

    db.delete(notification)
    db.commit()
    return schemas.MessageResponse(message="Notification deleted successfully")


@router.post("/device-token", response_model=schemas.DeviceTokenResponse, status_code=status.HTTP_201_CREATED)
def register_device_token(
    payload: schemas.DeviceTokenRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Registers an FCM device token for push notifications. Tokens are unique
    per device, not per user — if the same token was previously registered
    to a different account (e.g. a shared/reset device), it's reassigned to
    the current user rather than rejected.
    """
    existing = (
        db.query(models.DeviceToken)
        .filter(models.DeviceToken.token == payload.token)
        .first()
    )
    if existing is not None:
        existing.user_id = current_user.id
        existing.platform = payload.platform
        db.commit()
    else:
        db.add(
            models.DeviceToken(
                user_id=current_user.id,
                token=payload.token,
                platform=payload.platform,
            )
        )
        db.commit()

    return schemas.DeviceTokenResponse(
        message="Device token registered", token=payload.token, platform=payload.platform
    )


# ==========================================================================
# WebSocket — real-time notification delivery
# ==========================================================================
#
# Connect with:  ws(s)://<host>/api/notifications/ws?token=<access_token>
#
# Same access token you already use for REST calls — it travels as a query
# parameter because the browser/mobile WebSocket APIs can't set a custom
# Authorization header (same reason the chat socket at /api/chat/ws does
# this). The connection is closed with code 4401 if the token is missing,
# expired, or invalid.
#
# This socket is separate from the chat socket (/api/chat/ws) — connecting
# to one doesn't register you on the other, and you can hold either or both
# open at once.
#
# Client -> server messages (JSON):
#   {"type": "ping"}
#
# Server -> client messages (JSON):
#   {"type": "connected", "unread_count": 3}                     -- sent once, right after connecting
#   {"type": "notification", "notification": {...NotificationOut}}   -- pushed the instant a new notification is created
#   {"type": "pong"}
#   {"type": "error", "detail": "..."}
#
# The `notification` object is exactly the same shape as an item in
# GET /api/notifications' `items` list:
#   {
#     "id": 42,
#     "type": "follow" | "follow_request" | "like" | "comment" | "mention" | "share" | "message" | "other",
#     "actor_id": 7,
#     "message": "prasanna started following you",
#     "target_type": "user" | "story" | "follow_request" | ...,
#     "target_id": 7,
#     "is_read": false,
#     "created_at": "2026-09-02T10:15:00"
#   }
#
# This is purely additive delivery — the notification is always written to
# the DB first (unchanged GET /api/notifications behavior), and an FCM push
# is also sent to every registered device (see POST /device-token above and
# app/services/push_service.py) so notifications still arrive when the app
# is backgrounded or fully closed, i.e. not connected to this socket at all.

@router.websocket("/ws")
async def notifications_websocket(websocket: WebSocket, token: str = Query(...)):
    # Accept the handshake *before* authenticating. Per the ASGI spec, if an
    # app's first response is `websocket.close` (i.e. we reject before ever
    # accepting), a real ASGI server is required to answer with a generic
    # HTTP rejection — uvicorn sends a plain 403 and discards whatever close
    # code we passed. That's what was producing a 403 at the handshake here:
    # any early rejection (bad/expired token, or even a transient error
    # while looking the token up) surfaced as 403 instead of a proper
    # WebSocket close(4401) — and it also meant a valid connection was never
    # guaranteed to make it past that same pre-accept window cleanly.
    # Accepting unconditionally first, then closing with 4401 *after* the
    # handshake if auth fails, makes both paths deterministic: valid tokens
    # always connect and get "connected", invalid ones always get a real
    # WebSocket close(4401) that every WS client can observe (never a 403).
    await websocket.accept()
    db = SessionLocal()
    try:
        user = get_user_from_raw_token(token, db)
        if user is None:
            await websocket.close(code=4401)
            return

        user_id = user.id
        await notification_manager.register(user_id, websocket)

        unread_count = (
            db.query(models.Notification)
            .filter(models.Notification.user_id == user_id, models.Notification.is_read.is_(False))
            .count()
        )
        await websocket.send_json({"type": "connected", "unread_count": unread_count})

        try:
            while True:
                data = await websocket.receive_json()
                if data.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
                else:
                    await websocket.send_json(
                        {"type": "error", "detail": f"Unknown event type '{data.get('type')}'"}
                    )
        except WebSocketDisconnect:
            pass
        finally:
            await notification_manager.disconnect(user_id, websocket)
    finally:
        db.close()
