"""Long-form video comments/likes and owner analytics interaction events."""
from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app import models
from app.auth import get_current_user
from app.database import get_db

router = APIRouter(tags=["Video Engagement", "Content Interactions"])

class VideoCommentIn(BaseModel):
    content: str = Field(min_length=1, max_length=2200)

class InteractionIn(BaseModel):
    event_type: Literal["view", "progress", "share", "save"]
    watched_seconds: float = Field(default=0, ge=0, le=86400)

def _video(db: Session, video_id: int):
    row = db.query(models.Video).filter_by(id=video_id).first()
    if row is None:
        raise HTTPException(404, "Video not found")
    return row

@router.post("/api/videos/{video_id}/comments", status_code=201)
def create_video_comment(video_id: int, payload: VideoCommentIn, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    video = _video(db, video_id)
    if not video.allow_comments:
        raise HTTPException(403, "Comments are disabled for this video")
    row = models.VideoComment(video_id=video_id, user_id=user.id, content=payload.content.strip())
    if not row.content:
        raise HTTPException(400, "Comment cannot be empty")
    db.add(row); db.commit(); db.refresh(row)
    return {"id": row.id, "video_id": row.video_id, "user_id": row.user_id, "content": row.content, "created_at": row.created_at}

@router.get("/api/videos/{video_id}/comments")
def list_video_comments(video_id: int, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    _video(db, video_id)
    q = db.query(models.VideoComment).filter_by(video_id=video_id)
    total = q.count()
    rows = q.order_by(models.VideoComment.created_at.desc(), models.VideoComment.id.desc()).offset(offset).limit(limit).all()
    return {"total": total, "limit": limit, "offset": offset, "items": [{"id": r.id, "video_id": r.video_id, "user_id": r.user_id, "content": r.content, "created_at": r.created_at} for r in rows]}

@router.delete("/api/videos/{video_id}/comments/{comment_id}")
def delete_video_comment(video_id: int, comment_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    row = db.query(models.VideoComment).filter_by(id=comment_id, video_id=video_id).first()
    if row is None: raise HTTPException(404, "Comment not found")
    video = _video(db, video_id)
    if row.user_id != user.id and video.user_id != user.id: raise HTTPException(403, "Not allowed to delete this comment")
    db.delete(row); db.commit()
    return {"message": "Comment deleted"}

@router.post("/api/videos/{video_id}/like")
def like_video(video_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    _video(db, video_id)
    row = db.query(models.VideoLike).filter_by(video_id=video_id, user_id=user.id).first()
    if row is None:
        row = models.VideoLike(video_id=video_id, user_id=user.id); db.add(row); db.commit()
    count = db.query(models.VideoLike).filter_by(video_id=video_id).count()
    return {"message": "Video liked", "video_id": video_id, "liked": True, "likes_count": count}

@router.delete("/api/videos/{video_id}/like")
def unlike_video(video_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    _video(db, video_id)
    row = db.query(models.VideoLike).filter_by(video_id=video_id, user_id=user.id).first()
    if row is not None: db.delete(row); db.commit()
    count = db.query(models.VideoLike).filter_by(video_id=video_id).count()
    return {"message": "Video unliked", "video_id": video_id, "liked": False, "likes_count": count}

@router.get("/api/videos/{video_id}/likes")
def list_video_likes(video_id: int, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    _video(db, video_id)
    q = db.query(models.VideoLike).filter_by(video_id=video_id)
    total = q.count()
    rows = q.order_by(models.VideoLike.created_at.desc()).offset(offset).limit(limit).all()
    return {"total": total, "limit": limit, "offset": offset, "items": [{"user_id": r.user_id, "created_at": r.created_at} for r in rows]}

def _content(db: Session, content_type: str, content_id: int):
    model = {"post": models.Post, "reel": models.Reel, "video": models.Video}[content_type]
    row = db.query(model).filter_by(id=content_id).first()
    if row is None: raise HTTPException(404, f"{content_type.title()} not found")
    return row

@router.post("/api/analytics/content/{content_type}/{content_id}/interactions", status_code=201)
def record_interaction(content_type: Literal["post", "reel", "video"], content_id: int, payload: InteractionIn, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    content = _content(db, content_type, content_id)
    row = models.ContentInteractionEvent(content_type=content_type, content_id=content_id, owner_id=content.user_id, actor_id=user.id, event_type=payload.event_type, watched_seconds=payload.watched_seconds)
    db.add(row); db.commit(); db.refresh(row)
    return {"id": row.id, "content_type": content_type, "content_id": content_id, "event_type": row.event_type, "watched_seconds": row.watched_seconds, "created_at": row.created_at}

@router.get("/api/analytics/content/{content_type}/{content_id}/interactions")
def get_interactions(content_type: Literal["post", "reel", "video"], content_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    content = _content(db, content_type, content_id)
    if content.user_id != user.id: raise HTTPException(403, "Only the content owner can view interactions")
    rows = db.query(models.ContentInteractionEvent).filter_by(content_type=content_type, content_id=content_id, owner_id=user.id).all()
    counts = {kind: sum(1 for r in rows if r.event_type == kind) for kind in ("view", "progress", "share", "save")}
    return {"content_type": content_type, "content_id": content_id, "events_count": len(rows), "counts": counts, "watch_time_seconds": round(sum(r.watched_seconds for r in rows), 2), "items": [{"actor_id": r.actor_id, "event_type": r.event_type, "watched_seconds": r.watched_seconds, "created_at": r.created_at} for r in rows[-100:]]}
