"""Per-conversation disappearing-message and per-user chat theme settings."""
from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app import models
from app.auth import get_current_user
from app.database import get_db

router = APIRouter(prefix="/api/chat/conversations", tags=["Chat Settings"])
DURATIONS = {"off": 0, "24h": 86400, "7d": 604800, "90d": 7776000}
THEMES = {"default", "love", "ocean", "sunset", "lavender", "forest", "dark"}

def _participant(db: Session, conversation_id: int, user_id: int):
    row = db.query(models.ConversationParticipant).filter_by(
        conversation_id=conversation_id, user_id=user_id
    ).first()
    if row is None:
        raise HTTPException(403, "You are not a participant in this conversation")
    return row

class ExpiryUpdate(BaseModel):
    expiry: Literal["off", "24h", "7d", "90d"]

class ThemeUpdate(BaseModel):
    theme: str

@router.get("/{conversation_id}/expiry")
def get_expiry(conversation_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    _participant(db, conversation_id, user.id)
    row = db.query(models.ConversationDisappearingSetting).filter_by(conversation_id=conversation_id).first()
    seconds = row.duration_seconds if row else 0
    name = next((k for k, v in DURATIONS.items() if v == seconds), "off")
    return {"conversation_id": conversation_id, "expiry": name, "duration_seconds": seconds}

@router.put("/{conversation_id}/expiry")
def set_expiry(conversation_id: int, payload: ExpiryUpdate, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    _participant(db, conversation_id, user.id)
    row = db.query(models.ConversationDisappearingSetting).filter_by(conversation_id=conversation_id).first()
    if row is None:
        row = models.ConversationDisappearingSetting(conversation_id=conversation_id, duration_seconds=DURATIONS[payload.expiry], updated_by=user.id)
        db.add(row)
    else:
        row.duration_seconds = DURATIONS[payload.expiry]
        row.updated_by = user.id
        row.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"message": "Disappearing-message setting updated", "conversation_id": conversation_id, "expiry": payload.expiry, "duration_seconds": DURATIONS[payload.expiry]}

@router.get("/{conversation_id}/theme")
def get_theme(conversation_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    _participant(db, conversation_id, user.id)
    row = db.query(models.ConversationThemePreference).filter_by(conversation_id=conversation_id, user_id=user.id).first()
    return {"conversation_id": conversation_id, "theme": row.theme if row else "default", "scope": "user"}

@router.put("/{conversation_id}/theme")
def set_theme(conversation_id: int, payload: ThemeUpdate, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    _participant(db, conversation_id, user.id)
    theme = payload.theme.strip().lower()
    if theme not in THEMES:
        raise HTTPException(400, "theme must be one of: " + ", ".join(sorted(THEMES)))
    row = db.query(models.ConversationThemePreference).filter_by(conversation_id=conversation_id, user_id=user.id).first()
    if row is None:
        row = models.ConversationThemePreference(conversation_id=conversation_id, user_id=user.id, theme=theme)
        db.add(row)
    else:
        row.theme = theme
        row.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"message": "Conversation theme updated", "conversation_id": conversation_id, "theme": theme, "scope": "user"}
