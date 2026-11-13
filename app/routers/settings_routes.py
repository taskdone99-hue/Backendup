"""Account settings APIs: comments, mentions, hidden words and screen time.

Existing settings are intentionally reused rather than duplicated:
- Engagement: /api/users/me/engagement-settings (user_routes.py)
- Tags: /api/tags/settings (tag_routes.py)
- Blocked accounts: /api/privacy/blocked (privacy_routes.py)
- Theme: /api/users/me/theme (call_feature_routes.py)
"""
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db

router = APIRouter(tags=["Account Settings"])


class CommentSettingsUpdate(BaseModel):
    allow_comments_from: Literal["everyone", "followers", "following", "off"] | None = None
    filter_offensive_comments: bool | None = None
    manual_filter_enabled: bool | None = None

    @model_validator(mode="after")
    def require_field(self):
        if not self.model_fields_set:
            raise ValueError("Send at least one comment setting")
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Comment settings cannot be null")
        return self


class MentionSettingsUpdate(BaseModel):
    allow_mentions_from: Literal["everyone", "people_you_follow", "no_one"]


class HiddenWordCreate(BaseModel):
    word: str = Field(min_length=1, max_length=100)

    @field_validator("word")
    @classmethod
    def clean_word(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("word cannot be blank")
        return value


class ScreenTimeSessionCreate(BaseModel):
    client_session_id: str = Field(min_length=1, max_length=100)
    duration_seconds: int = Field(ge=1, le=86400)
    session_date: date | None = None
    started_at: datetime | None = None


@router.get("/api/users/me/comment-settings")
def get_comment_settings(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    row = db.query(models.UserCommentSettings).filter_by(user_id=user.id).first()
    return {
        "allow_comments_from": row.allow_comments_from if row else "everyone",
        "filter_offensive_comments": row.filter_offensive_comments if row else False,
        "manual_filter_enabled": row.manual_filter_enabled if row else False,
    }


@router.put("/api/users/me/comment-settings")
def update_comment_settings(payload: CommentSettingsUpdate, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    row = db.query(models.UserCommentSettings).filter_by(user_id=user.id).first()
    if row is None:
        row = models.UserCommentSettings(user_id=user.id)
        db.add(row)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return {"message": "Comment settings updated", "allow_comments_from": row.allow_comments_from,
            "filter_offensive_comments": row.filter_offensive_comments, "manual_filter_enabled": row.manual_filter_enabled}


@router.get("/api/users/me/mention-settings")
def get_mention_settings(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    row = db.query(models.UserMentionSettings).filter_by(user_id=user.id).first()
    return {"allow_mentions_from": row.allow_mentions_from if row else "everyone"}


@router.put("/api/users/me/mention-settings")
def update_mention_settings(payload: MentionSettingsUpdate, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    row = db.query(models.UserMentionSettings).filter_by(user_id=user.id).first()
    if row is None:
        row = models.UserMentionSettings(user_id=user.id)
        db.add(row)
    row.allow_mentions_from = payload.allow_mentions_from
    db.commit()
    db.refresh(row)
    return {"message": "Mention settings updated", "allow_mentions_from": row.allow_mentions_from}


@router.get("/api/users/me/hidden-words")
def list_hidden_words(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    rows = db.query(models.UserHiddenWord).filter_by(user_id=user.id).order_by(models.UserHiddenWord.id.desc()).all()
    return {"total": len(rows), "items": [{"id": r.id, "word": r.word, "created_at": r.created_at} for r in rows]}


@router.post("/api/users/me/hidden-words", status_code=status.HTTP_201_CREATED)
def add_hidden_word(payload: HiddenWordCreate, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    existing = db.query(models.UserHiddenWord).filter(
        models.UserHiddenWord.user_id == user.id,
        func.lower(models.UserHiddenWord.word) == payload.word.lower(),
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="This hidden word already exists")
    row = models.UserHiddenWord(user_id=user.id, word=payload.word)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="This hidden word already exists")
    db.refresh(row)
    return {"message": "Hidden word added", "id": row.id, "word": row.word, "created_at": row.created_at}


@router.delete("/api/users/me/hidden-words/{word_id}")
def delete_hidden_word(word_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    row = db.query(models.UserHiddenWord).filter_by(id=word_id, user_id=user.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Hidden word not found")
    db.delete(row)
    db.commit()
    return {"message": "Hidden word deleted", "id": word_id}


@router.post("/api/users/me/time-spent/sessions", status_code=status.HTTP_201_CREATED)
def record_screen_time(payload: ScreenTimeSessionCreate, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Record one client-measured foreground session; client_session_id makes retries idempotent."""
    existing = db.query(models.UserScreenTimeSession).filter_by(
        user_id=user.id, client_session_id=payload.client_session_id
    ).first()
    if existing:
        return {"message": "Session already recorded", "session_id": existing.id,
                "duration_seconds": existing.duration_seconds, "duplicate": True}
    row = models.UserScreenTimeSession(
        user_id=user.id,
        client_session_id=payload.client_session_id,
        duration_seconds=payload.duration_seconds,
        session_date=payload.session_date or date.today(),
        started_at=payload.started_at,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Session already recorded")
    db.refresh(row)
    return {"message": "Screen time recorded", "session_id": row.id,
            "duration_seconds": row.duration_seconds, "session_date": row.session_date, "duplicate": False}


@router.get("/api/users/me/time-spent")
def get_screen_time(days: int = Query(7, ge=1, le=90), db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    start_date = date.today() - timedelta(days=days - 1)
    rows = db.query(
        models.UserScreenTimeSession.session_date,
        func.sum(models.UserScreenTimeSession.duration_seconds).label("total_seconds"),
    ).filter(
        models.UserScreenTimeSession.user_id == user.id,
        models.UserScreenTimeSession.session_date >= start_date,
    ).group_by(models.UserScreenTimeSession.session_date).order_by(models.UserScreenTimeSession.session_date.asc()).all()
    daily = [{"date": day.isoformat(), "duration_seconds": int(total or 0)} for day, total in rows]
    total = sum(item["duration_seconds"] for item in daily)
    return {"period_days": days, "total_seconds": total, "total_minutes": round(total / 60, 2), "daily": daily}
