
"""Authenticated messaging preferences API (v1)."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import get_current_user
from app.database import get_db

router = APIRouter(
    prefix="/v1/me/message-settings",
    tags=["message settings"],
)

DEFAULTS = {
    "allow_messages_from": "everyone",
    "read_receipts": True,
    "activity_status": False,
    "filter_message_requests": True,
    "group_add_permission": "following",
}


def _get_or_create_settings(
    db: Session,
    user_id: int,
) -> models.UserMessageSettings:
    row = db.query(models.UserMessageSettings).filter(
        models.UserMessageSettings.user_id == user_id
    ).first()

    if row is None:
        row = models.UserMessageSettings(user_id=user_id, **DEFAULTS)
        db.add(row)
        db.flush()

    return row


def _serialize(
    row,
) -> schemas.MessageSettingsOut:
    return schemas.MessageSettingsOut(
        allowMessagesFrom=row.allow_messages_from,
        readReceipts=row.read_receipts,
        activityStatus=row.activity_status,
        filterMessageRequests=row.filter_message_requests,
        groupAddPermission=row.group_add_permission,
    )


@router.get("", response_model=schemas.MessageSettingsOut)
def get_message_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = db.query(models.UserMessageSettings).filter(
        models.UserMessageSettings.user_id == current_user.id
    ).first()

    if row is None:
        # GET is read-only; return defaults without inserting a row.
        class DefaultsView:
            pass

        row = DefaultsView()
        for key, value in DEFAULTS.items():
            setattr(row, key, value)

    return _serialize(row)


@router.patch("", response_model=schemas.MessageSettingsOut)
def update_message_settings(
    payload: schemas.MessageSettingsPatch,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    updates = payload.model_dump(exclude_unset=True)

    if not updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide at least one message setting to update",
        )

    if any(value is None for value in updates.values()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Message settings cannot be null",
        )

    row = _get_or_create_settings(db, current_user.id)

    field_map = {
        "allowMessagesFrom": "allow_messages_from",
        "readReceipts": "read_receipts",
        "activityStatus": "activity_status",
        "filterMessageRequests": "filter_message_requests",
        "groupAddPermission": "group_add_permission",
    }

    old_values = {}
    new_values = {}

    for api_name, model_name in field_map.items():
        if api_name not in updates:
            continue

        old = getattr(row, model_name)
        new = updates[api_name]

        if old != new:
            old_values[api_name] = old
            new_values[api_name] = new
            setattr(row, model_name, new)

    try:
        if new_values:
            db.add(models.MessageSettingsAudit(
                user_id=current_user.id,
                changed_fields=list(new_values.keys()),
                old_values=old_values,
                new_values=new_values,
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent", "")[:500] or None,
            ))

        db.commit()
        db.refresh(row)

    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to save message settings",
        )

    return _serialize(row)
