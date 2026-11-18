"""Independent long-form Videos API. Videos are stored in the `videos` table,
not in `reels`; Reel endpoints and engagement remain separate.
"""
from pathlib import Path
from typing import Literal
import mimetypes

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db
from app.services.media_service import (
    STATIC_ROOT, _local_path_from_url, delete_media_file,
    generate_video_thumbnail, get_video_metadata, save_upload_file,
)

router = APIRouter(prefix="/api/videos", tags=["long-form videos"])
VIDEO_MIN_SECONDS = 300
VIDEO_MAX_SECONDS = 420
VIDEO_MAX_BYTES = 500 * 1024 * 1024


class VideoCreate(BaseModel):
    video_url: str = Field(..., min_length=1, max_length=500)
    thumbnail_url: str | None = Field(default=None, max_length=500)
    title: str = Field(..., min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=5000)
    visibility: Literal["public", "private"] = "public"
    allow_comments: bool = True
    allow_sharing: bool = True
    location: str | None = Field(default=None, max_length=150)
    category: str | None = Field(default=None, max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title is required")
        return value

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(v.strip().lstrip("#") for v in values if v.strip()))


class VideoUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=5000)
    visibility: Literal["public", "private"] | None = None
    allow_comments: bool | None = None
    allow_sharing: bool | None = None
    location: str | None = Field(default=None, max_length=150)
    category: str | None = Field(default=None, max_length=80)
    tags: list[str] | None = Field(default=None, max_length=30)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("title cannot be blank")
        return value

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        return list(dict.fromkeys(v.strip().lstrip("#") for v in values if v.strip()))


def _validate_video_asset(url: str) -> tuple[Path, dict]:
    path = _local_path_from_url(url)
    if path is None:
        raise HTTPException(status_code=400, detail="video_url must reference an uploaded local video")
    try:
        path.resolve().relative_to((STATIC_ROOT / "videos").resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="video_url must reference the videos upload folder")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=400, detail="Uploaded video file was not found")
    metadata = get_video_metadata(url)
    if metadata is None:
        raise HTTPException(status_code=400, detail="Could not read video metadata; ensure ffprobe is installed")
    duration = metadata["duration_seconds"]
    if not VIDEO_MIN_SECONDS <= duration <= VIDEO_MAX_SECONDS:
        raise HTTPException(status_code=400, detail="Long-form videos must be between 300 and 420 seconds (5–7 minutes)")
    if metadata["width"] < metadata["height"]:
        raise HTTPException(status_code=400, detail="Long-form videos must be horizontal (width must be greater than or equal to height)")
    return path, metadata


def _out(row: models.Video) -> dict:
    return {
        "id": row.id, "user_id": row.user_id, "title": row.title,
        "description": row.description, "video_url": row.video_url,
        "thumbnail_url": row.thumbnail_url, "mime_type": row.mime_type,
        "file_size": row.file_size, "duration_seconds": row.duration_seconds,
        "width": row.width, "height": row.height, "aspect_ratio": row.aspect_ratio,
        "orientation": row.orientation, "visibility": row.visibility,
        "allow_comments": row.allow_comments, "allow_sharing": row.allow_sharing,
        "location": row.location, "category": row.category, "tags": row.tags or [],
        "created_at": row.created_at, "updated_at": row.updated_at,
    }


def _owned(db: Session, video_id: int, user: models.User) -> models.Video:
    row = db.query(models.Video).filter(models.Video.id == video_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Video not found")
    if row.user_id != user.id:
        raise HTTPException(status_code=403, detail="You can only manage your own videos")
    return row


@router.post("/upload", status_code=status.HTTP_201_CREATED)
def upload_video_media(
    file: UploadFile = File(..., description="5–7 minute horizontal video from gallery"),
    thumbnail: UploadFile | None = File(default=None, description="Optional custom poster image"),
    current_user: models.User = Depends(get_current_user),
):
    url, kind = save_upload_file(file, "videos", allow_video=True, max_video_bytes=VIDEO_MAX_BYTES)
    if kind != "video":
        delete_media_file(url)
        raise HTTPException(status_code=400, detail="File must be a video")
    try:
        path, metadata = _validate_video_asset(url)
        thumb_url = None
        if thumbnail is not None and thumbnail.filename:
            thumb_url, thumb_kind = save_upload_file(thumbnail, "video_thumbnails", allow_video=False)
            if thumb_kind != "image":
                delete_media_file(url)
                raise HTTPException(status_code=400, detail="Thumbnail must be an image")
        else:
            thumb_url = generate_video_thumbnail(url, subfolder="video_thumbnails")
        return {
            "message": "Video media uploaded successfully",
            "video_url": url, "thumbnail_url": thumb_url,
            "mime_type": file.content_type or "video/mp4",
            "file_size": path.stat().st_size, **metadata,
            "orientation": "horizontal",
            "aspect_ratio": f"{metadata['width']}:{metadata['height']}",
        }
    except Exception:
        delete_media_file(url)
        raise


@router.post("", status_code=status.HTTP_201_CREATED)
def create_video(
    payload: VideoCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    path, metadata = _validate_video_asset(payload.video_url)
    thumbnail_url = payload.thumbnail_url
    if thumbnail_url:
        thumb_path = _local_path_from_url(thumbnail_url)
        try:
            if thumb_path is None:
                raise ValueError()
            thumb_path.resolve().relative_to((STATIC_ROOT / "video_thumbnails").resolve())
            if not thumb_path.is_file():
                raise ValueError()
        except ValueError:
            raise HTTPException(status_code=400, detail="thumbnail_url must reference an uploaded video thumbnail")
    else:
        thumbnail_url = generate_video_thumbnail(payload.video_url, subfolder="video_thumbnails")
    row = models.Video(
        user_id=current_user.id, title=payload.title, description=payload.description,
        video_url=payload.video_url, thumbnail_url=thumbnail_url,
        mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        file_size=path.stat().st_size,
        duration_seconds=metadata["duration_seconds"], width=metadata["width"],
        height=metadata["height"], aspect_ratio=f"{metadata['width']}:{metadata['height']}",
        orientation="horizontal", visibility=payload.visibility,
        allow_comments=payload.allow_comments, allow_sharing=payload.allow_sharing,
        location=payload.location, category=payload.category, tags=payload.tags,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _out(row)


@router.get("")
def list_videos(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    query = db.query(models.Video).filter(
        (models.Video.visibility == "public") | (models.Video.user_id == current_user.id)
    )
    total = query.count()
    rows = query.order_by(models.Video.created_at.desc(), models.Video.id.desc()).offset(offset).limit(limit).all()
    return {"total": total, "limit": limit, "offset": offset, "items": [_out(row) for row in rows]}


@router.get("/mine")
def my_videos(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    query = db.query(models.Video).filter(models.Video.user_id == current_user.id)
    total = query.count()
    rows = query.order_by(models.Video.created_at.desc(), models.Video.id.desc()).offset(offset).limit(limit).all()
    return {"total": total, "limit": limit, "offset": offset, "items": [_out(row) for row in rows]}


@router.get("/{video_id}")
def get_video(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = db.query(models.Video).filter(models.Video.id == video_id).first()
    if row is None or (row.visibility == "private" and row.user_id != current_user.id):
        raise HTTPException(status_code=404, detail="Video not found")
    return _out(row)


@router.patch("/{video_id}")
def update_video(
    video_id: int,
    payload: VideoUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = _owned(db, video_id, current_user)
    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return _out(row)


@router.put("/{video_id}/media")
def replace_video_media(
    video_id: int,
    file: UploadFile = File(...),
    thumbnail: UploadFile | None = File(default=None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = _owned(db, video_id, current_user)
    new_url, kind = save_upload_file(file, "videos", allow_video=True, max_video_bytes=VIDEO_MAX_BYTES)
    if kind != "video":
        delete_media_file(new_url)
        raise HTTPException(status_code=400, detail="File must be a video")
    try:
        path, metadata = _validate_video_asset(new_url)
        if thumbnail is not None and thumbnail.filename:
            new_thumb, thumb_kind = save_upload_file(thumbnail, "video_thumbnails", allow_video=False)
            if thumb_kind != "image":
                delete_media_file(new_url)
                raise HTTPException(status_code=400, detail="Thumbnail must be an image")
        else:
            new_thumb = generate_video_thumbnail(new_url, subfolder="video_thumbnails")
        old_url, old_thumb = row.video_url, row.thumbnail_url
        row.video_url = new_url
        row.thumbnail_url = new_thumb
        row.mime_type = file.content_type or "video/mp4"
        row.file_size = path.stat().st_size
        row.duration_seconds = metadata["duration_seconds"]
        row.width, row.height = metadata["width"], metadata["height"]
        row.aspect_ratio = f"{metadata['width']}:{metadata['height']}"
        row.orientation = "horizontal"
        db.commit()
        db.refresh(row)
        delete_media_file(old_url)
        if old_thumb:
            delete_media_file(old_thumb)
        return _out(row)
    except Exception:
        delete_media_file(new_url)
        raise


@router.put("/{video_id}/thumbnail")
def replace_video_thumbnail(
    video_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = _owned(db, video_id, current_user)
    url, kind = save_upload_file(file, "video_thumbnails", allow_video=False)
    if kind != "image":
        delete_media_file(url)
        raise HTTPException(status_code=400, detail="Thumbnail must be an image")
    old = row.thumbnail_url
    row.thumbnail_url = url
    db.commit()
    db.refresh(row)
    if old:
        delete_media_file(old)
    return {"message": "Thumbnail updated", "thumbnail_url": url, "video": _out(row)}


@router.delete("/{video_id}")
def delete_video(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = _owned(db, video_id, current_user)
    video_url, thumbnail_url = row.video_url, row.thumbnail_url
    db.delete(row)
    db.commit()
    delete_media_file(video_url)
    if thumbnail_url:
        delete_media_file(thumbnail_url)
    return {"message": "Video deleted successfully", "video_id": video_id}
