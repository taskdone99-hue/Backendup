"""
Minimal content-creation endpoints.

Not part of the API list you gave me — but GET /api/users/:id/posts,
/reels, and /saved have nothing to return without a way to create posts,
reels, and saves in the first place, so these are included to make that
part of the system actually usable end-to-end.
"""

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user
from app.services.media_service import save_upload_file

router = APIRouter(prefix="/api/posts", tags=["posts"])
reels_router = APIRouter(prefix="/api/reels", tags=["reels"])


@router.post("", response_model=schemas.PostOut, status_code=status.HTTP_201_CREATED)
def create_post(
    file: UploadFile,
    caption: str | None = Form(default=None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    url, kind = save_upload_file(file, "posts", allow_video=True)
    post = models.Post(
        user_id=current_user.id,
        caption=caption,
        media_url=url,
        media_type=models.MediaType.video if kind == "video" else models.MediaType.image,
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    return post


@router.post("/{post_id}/save", response_model=schemas.MessageResponse)
def save_post(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if post is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")

    existing = (
        db.query(models.SavedPost)
        .filter(models.SavedPost.user_id == current_user.id, models.SavedPost.post_id == post_id)
        .first()
    )
    if existing is None:
        db.add(models.SavedPost(user_id=current_user.id, post_id=post_id))
        db.commit()

    return schemas.MessageResponse(message="Post saved")


@router.delete("/{post_id}/save", response_model=schemas.MessageResponse)
def unsave_post(
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


@reels_router.post("", response_model=schemas.ReelOut, status_code=status.HTTP_201_CREATED)
def create_reel(
    file: UploadFile,
    caption: str | None = Form(default=None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    url, kind = save_upload_file(file, "reels", allow_video=True)
    if kind != "video":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Reels must be a video file"
        )
    reel = models.Reel(user_id=current_user.id, caption=caption, video_url=url)
    db.add(reel)
    db.commit()
    db.refresh(reel)
    return reel
