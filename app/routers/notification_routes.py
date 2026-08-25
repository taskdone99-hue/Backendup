from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user

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
