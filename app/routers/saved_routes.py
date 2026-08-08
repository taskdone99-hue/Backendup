"""
Saved Posts under the /api/saved prefix. This is a second, spec-mandated
entry point onto the same `saved_posts` table that
POST/DELETE /api/posts/:id/save already write — both stay in sync since
they share models.SavedPost.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user

router = APIRouter(prefix="/api/saved", tags=["saved"])


def _get_post_or_404(db: Session, post_id: int) -> models.Post:
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if post is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    return post


@router.post("", response_model=schemas.MessageResponse, status_code=status.HTTP_201_CREATED)
def save_post(
    payload: schemas.SavePostRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _get_post_or_404(db, payload.post_id)

    existing = (
        db.query(models.SavedPost)
        .filter(
            models.SavedPost.user_id == current_user.id,
            models.SavedPost.post_id == payload.post_id,
        )
        .first()
    )
    if existing is None:
        db.add(models.SavedPost(user_id=current_user.id, post_id=payload.post_id))
        db.commit()

    return schemas.MessageResponse(message="Post saved")


@router.delete("/{post_id}", response_model=schemas.MessageResponse)
def remove_saved_post(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    existing = (
        db.query(models.SavedPost)
        .filter(models.SavedPost.user_id == current_user.id, models.SavedPost.post_id == post_id)
        .first()
    )
    if existing is not None:
        db.delete(existing)
        db.commit()

    return schemas.MessageResponse(message="Post unsaved")


@router.get("", response_model=schemas.PaginatedPostsResponse)
def get_saved_posts(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    query = (
        db.query(models.Post)
        .join(models.SavedPost, models.SavedPost.post_id == models.Post.id)
        .filter(models.SavedPost.user_id == current_user.id)
    )
    total = query.count()
    items = (
        query.order_by(models.SavedPost.created_at.desc()).offset(offset).limit(limit).all()
    )
    return schemas.PaginatedPostsResponse(total=total, limit=limit, offset=offset, items=items)
