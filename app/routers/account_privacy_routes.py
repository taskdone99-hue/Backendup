"""Authenticated account privacy settings API (v1)."""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import get_current_user
from app.database import get_db

router = APIRouter(prefix="/v1/me/privacy-settings", tags=["account privacy"])

DEFAULTS = {
    "show_activity_status": True,
    "show_follower_list": True,
    "show_online_status": False,
    "account_suggestions": True,
}


def _get_or_create_settings(db: Session, user_id: int) -> models.UserPrivacySettings:
    row = db.query(models.UserPrivacySettings).filter(
        models.UserPrivacySettings.user_id == user_id
    ).first()
    if row is None:
        row = models.UserPrivacySettings(user_id=user_id, **DEFAULTS)
        db.add(row)
        db.flush()
    return row


def _serialize(user: models.User, row: models.UserPrivacySettings) -> schemas.PrivacySettingsOut:
    return schemas.PrivacySettingsOut(
        visibility="private" if user.is_private else "public",
        showActivityStatus=row.show_activity_status,
        showFollowerList=row.show_follower_list,
        showOnlineStatus=row.show_online_status,
        accountSuggestions=row.account_suggestions,
    )


@router.get("", response_model=schemas.PrivacySettingsOut)
def get_privacy_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = db.query(models.UserPrivacySettings).filter(
        models.UserPrivacySettings.user_id == current_user.id
    ).first()
    if row is None:
        # GET is read-only; defaults are returned without writing a row.
        class Defaults:  # tiny view object to keep serialization centralized
            pass
        row = Defaults()
        for key, value in DEFAULTS.items():
            setattr(row, key, value)
    return _serialize(current_user, row)


@router.patch("", response_model=schemas.PrivacySettingsOut)
def update_privacy_settings(
    payload: schemas.PrivacySettingsPatch,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    updates = payload.model_dump(exclude_unset=True)
    # Pydantic permits explicit null for optional patch fields; reject it rather
    # than silently converting it or persisting an invalid nullable preference.
    if any(value is None for value in updates.values()):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Privacy settings cannot be null")

    row = _get_or_create_settings(db, current_user.id)
    old_values = {}
    new_values = {}

    if "visibility" in updates:
        old_visibility = "private" if current_user.is_private else "public"
        new_visibility = updates["visibility"]
        if old_visibility != new_visibility:
            old_values["visibility"] = old_visibility
            new_values["visibility"] = new_visibility
            current_user.is_private = new_visibility == "private"

    field_map = {
        "showActivityStatus": "show_activity_status",
        "showFollowerList": "show_follower_list",
        "showOnlineStatus": "show_online_status",
        "accountSuggestions": "account_suggestions",
    }
    for api_name, model_name in field_map.items():
        if api_name in updates:
            old = bool(getattr(row, model_name))
            new = updates[api_name]
            if old != new:
                old_values[api_name] = old
                new_values[api_name] = new
                setattr(row, model_name, new)

    try:
        if new_values:
            db.add(models.PrivacySettingsAudit(
                user_id=current_user.id,
                changed_fields=list(new_values.keys()),
                old_values=old_values,
                new_values=new_values,
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent", "")[:500] or None,
            ))
        db.commit()
        db.refresh(current_user)
        db.refresh(row)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Unable to save privacy settings")

    return _serialize(current_user, row)
