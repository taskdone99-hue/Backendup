"""Independent long-form Videos API. Videos are stored in the `videos` table,
not in `reels`; Reel endpoints and engagement remain separate.
"""
from pathlib import Path
from typing import Literal
import mimetypes
import json

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
from app.services import editor_state_service

router = APIRouter(prefix="/api/videos", tags=["long-form videos"])
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
    editor_state: dict | None = None
    audio_id: int | None = Field(default=None, gt=0)
    recording_duration_seconds: float | None = Field(default=None, ge=0, le=3600)
    timer_duration_seconds: float | None = Field(default=None, ge=0, le=60)
    filter_id: str | None = Field(default=None, max_length=50)
    playback_speed: float | None = Field(default=None, gt=0, le=4)
    audio_start_seconds: float | None = Field(default=None, ge=0)
    tag_user_ids: list[int] = Field(default_factory=list)
    source: str | None = Field(default=None, max_length=30)
    location_id: int | None = Field(default=None, gt=0)
    location_name: str | None = Field(default=None, max_length=150)
    location_coordinates: dict | None = None

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
    editor_state: dict | None = None
    audio_id: int | None = Field(default=None, gt=0)
    recording_duration_seconds: float | None = Field(default=None, ge=0, le=3600)
    timer_duration_seconds: float | None = Field(default=None, ge=0, le=60)
    filter_id: str | None = Field(default=None, max_length=50)
    playback_speed: float | None = Field(default=None, gt=0, le=4)
    audio_start_seconds: float | None = Field(default=None, ge=0)
    tag_user_ids: list[int] | None = None
    source: str | None = Field(default=None, max_length=30)
    location_id: int | None = Field(default=None, gt=0)
    location_name: str | None = Field(default=None, max_length=150)
    location_coordinates: dict | None = None

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
    if duration > VIDEO_MAX_SECONDS:
        raise HTTPException(status_code=400, detail="Videos must be no longer than 420 seconds (7 minutes)")
    if metadata["width"] <= metadata["height"]:
        raise HTTPException(status_code=400, detail="Videos must be horizontal (width must be greater than height)")
    return path, metadata


def _out(row: models.Video, db: Session | None = None, viewer_id: int | None = None) -> dict:
    owner = row.user
    save_count_hidden = bool(owner and owner.hide_saved_count and viewer_id != row.user_id)
    saves_count = None
    if db is not None and not save_count_hidden:
        saves_count = db.query(models.VideoSavedItem).filter_by(video_id=row.id).count()
    return {
        "id": row.id, "user_id": row.user_id, "title": row.title,
        "description": row.description, "video_url": row.video_url,
        "thumbnail_url": row.thumbnail_url, "mime_type": row.mime_type,
        "file_size": row.file_size, "duration_seconds": row.duration_seconds,
        "width": row.width, "height": row.height, "aspect_ratio": row.aspect_ratio,
        "orientation": row.orientation, "visibility": row.visibility,
        "allow_comments": row.allow_comments, "allow_sharing": row.allow_sharing,
        "location": row.location,
        "location_id": row.location_id,
        "location_name": row.location_name or row.location,
        "location_coordinates": (
            {"latitude": row.location_latitude, "longitude": row.location_longitude}
            if row.location_latitude is not None or row.location_longitude is not None else None
        ),
        "category": row.category, "tags": row.tags or [],
        "share_count": None,
        "share_count_hidden": bool(owner and owner.hide_share_count and viewer_id != row.user_id),
        "saved_count": saves_count,
        "save_count_hidden": save_count_hidden,
        "editor_state": row.editor_state,
        "audio_id": row.audio_id,
        "recording_duration_seconds": row.recording_duration_seconds,
        "timer_duration_seconds": row.timer_duration_seconds,
        "filter_id": row.filter_id,
        "playback_speed": row.playback_speed,
        "audio_start_seconds": row.audio_start_seconds,
        "tag_user_ids": row.tag_user_ids or [],
        "source": row.source,
        "created_at": row.created_at, "updated_at": row.updated_at,
    }


def _owned(db: Session, video_id: int, user: models.User) -> models.Video:
    row = db.query(models.Video).filter(models.Video.id == video_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Video not found")
    if row.user_id != user.id:
        raise HTTPException(status_code=403, detail="You can only manage your own videos")
    return row


@router.post("", status_code=status.HTTP_201_CREATED)
def create_video(
    file: UploadFile = File(..., description="Upload a horizontal video up to 7 minutes (MP4 recommended)"),
    title: str = Form(..., min_length=1, max_length=150),
    description: str | None = Form(default=None, max_length=5000),
    thumbnail: UploadFile | None = File(default=None, description="Optional custom thumbnail; generated automatically if omitted"),
    visibility: Literal["public", "private"] = Form(default="public"),
    allow_comments: bool = Form(default=True),
    allow_sharing: bool = Form(default=True),
    location: str | None = Form(default=None, max_length=150),
    category: str | None = Form(default=None, max_length=80),
    tags: str | None = Form(default=None, description="Comma-separated tags or a JSON string array"),
    editor_state: str | None = Form(default=None, description="JSON object with editor state"),
    audio_id: int | None = Form(default=None, gt=0),
    recording_duration_seconds: float | None = Form(default=None, ge=0, le=3600),
    timer_duration_seconds: float | None = Form(default=None, ge=0, le=60),
    filter_id: str | None = Form(default=None, max_length=50),
    playback_speed: float | None = Form(default=None, gt=0, le=4),
    audio_start_seconds: float | None = Form(default=None, ge=0),
    tag_user_ids: str | None = Form(default=None, description="JSON array or comma-separated user IDs"),
    source: str | None = Form(default=None, max_length=30),
    location_id: int | None = Form(default=None, gt=0),
    location_name: str | None = Form(default=None, max_length=150),
    location_coordinates: str | None = Form(default=None, description="JSON object: latitude, longitude"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Create a long-form video in one multipart request, like POST /api/reels.

    The client uploads `file`; it does not send a video_url. The server stores
    the file, reads its metadata, generates a thumbnail when needed, and then
    creates the videos-table record.
    """
    clean_title = title.strip()
    if not clean_title:
        raise HTTPException(status_code=422, detail="title is required")

    parsed_tags: list[str] = []
    if tags:
        raw_tags = tags.strip()
        try:
            decoded = json.loads(raw_tags)
            if isinstance(decoded, list):
                parsed_tags = [str(item).strip().lstrip("#") for item in decoded if str(item).strip()]
            else:
                parsed_tags = [part.strip().lstrip("#") for part in raw_tags.split(",") if part.strip()]
        except json.JSONDecodeError:
            parsed_tags = [part.strip().lstrip("#") for part in raw_tags.split(",") if part.strip()]
    parsed_tags = list(dict.fromkeys(parsed_tags))
    if len(parsed_tags) > 30:
        raise HTTPException(status_code=422, detail="A maximum of 30 tags is allowed")

    parsed_editor_state = editor_state_service.parse_form_value(editor_state)
    parsed_tag_user_ids = []
    if tag_user_ids:
        try:
            raw_ids = json.loads(tag_user_ids)
            if not isinstance(raw_ids, list):
                raise ValueError
            parsed_tag_user_ids = [int(value) for value in raw_ids]
        except (json.JSONDecodeError, TypeError, ValueError):
            try:
                parsed_tag_user_ids = [int(value.strip()) for value in tag_user_ids.split(",") if value.strip()]
            except ValueError:
                raise HTTPException(status_code=422, detail="tag_user_ids must be a JSON array or comma-separated IDs")
    parsed_tag_user_ids = list(dict.fromkeys(parsed_tag_user_ids))
    if any(value <= 0 for value in parsed_tag_user_ids):
        raise HTTPException(status_code=422, detail="tag_user_ids must contain positive IDs")
    parsed_coordinates = None
    if location_coordinates:
        try:
            parsed_coordinates = json.loads(location_coordinates)
            if not isinstance(parsed_coordinates, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(status_code=422, detail="location_coordinates must be a JSON object")

    video_url = None
    thumbnail_url = None
    try:
        video_url, kind = save_upload_file(
            file, "videos", allow_video=True, max_video_bytes=VIDEO_MAX_BYTES
        )
        if kind != "video":
            raise HTTPException(status_code=400, detail="File must be a video")

        path, metadata = _validate_video_asset(video_url)
        if thumbnail is not None and thumbnail.filename:
            thumbnail_url, thumb_kind = save_upload_file(
                thumbnail, "video_thumbnails", allow_video=False
            )
            if thumb_kind != "image":
                raise HTTPException(status_code=400, detail="Thumbnail must be an image")
        else:
            thumbnail_url = generate_video_thumbnail(video_url, subfolder="video_thumbnails")

        row = models.Video(
            user_id=current_user.id,
            title=clean_title,
            description=description.strip() if description else None,
            video_url=video_url,
            thumbnail_url=thumbnail_url,
            mime_type=file.content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            file_size=path.stat().st_size,
            duration_seconds=metadata["duration_seconds"],
            width=metadata["width"],
            height=metadata["height"],
            aspect_ratio=f"{metadata['width']}:{metadata['height']}",
            orientation="horizontal",
            visibility=visibility,
            allow_comments=allow_comments,
            allow_sharing=allow_sharing,
            location=location.strip() if location else None,
            location_name=location_name.strip() if location_name else (location.strip() if location else None),
            location_id=location_id,
            location_latitude=(parsed_coordinates or {}).get("latitude"),
            location_longitude=(parsed_coordinates or {}).get("longitude"),
            category=category.strip() if category else None,
            tags=parsed_tags,
            editor_state=parsed_editor_state,
            audio_id=audio_id,
            recording_duration_seconds=recording_duration_seconds,
            timer_duration_seconds=timer_duration_seconds,
            filter_id=filter_id,
            playback_speed=playback_speed,
            audio_start_seconds=audio_start_seconds,
            tag_user_ids=parsed_tag_user_ids,
            source=source,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return _out(row, db, current_user.id)
    except Exception:
        db.rollback()
        if video_url:
            delete_media_file(video_url)
        if thumbnail_url:
            delete_media_file(thumbnail_url)
        raise


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
    return {"total": total, "limit": limit, "offset": offset, "items": [_out(row, db, current_user.id) for row in rows]}


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
    return {"total": total, "limit": limit, "offset": offset, "items": [_out(row, db, current_user.id) for row in rows]}


@router.get("/{video_id}/audience-options")
def get_video_audience_options(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Choices used by the Share Video audience picker."""
    _owned(db, video_id, current_user)
    return {
        "audiences": [
            {"value": "everyone", "label": "Everyone"},
            {"value": "followers", "label": "Followers"},
            {"value": "close_friends", "label": "Close Friends"},
        ],
        "age_restrictions": [
            {"value": "below_18", "label": "All ages"},
            {"value": "18_plus", "label": "18+"},
        ],
    }


@router.get("/{video_id}")
def get_video(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = db.query(models.Video).filter(models.Video.id == video_id).first()
    if row is None or (row.visibility == "private" and row.user_id != current_user.id):
        raise HTTPException(status_code=404, detail="Video not found")
    return _out(row, db, current_user.id)


@router.patch("/{video_id}")
@router.put("/{video_id}")
def update_video(
    video_id: int,
    payload: VideoUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = _owned(db, video_id, current_user)
    updates = payload.model_dump(exclude_unset=True)
    coordinates = updates.pop("location_coordinates", None)
    if "editor_state" in updates and updates["editor_state"] is not None:
        updates["editor_state"] = editor_state_service.check_object(updates["editor_state"])
    for key, value in updates.items():
        setattr(row, key, value)
    if coordinates is not None:
        row.location_latitude = coordinates.get("latitude")
        row.location_longitude = coordinates.get("longitude")
    if "location_name" in updates:
        row.location = updates["location_name"]
    db.commit()
    db.refresh(row)
    return _out(row, db, current_user.id)


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
        return _out(row, db, current_user.id)
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
    return {"message": "Thumbnail updated", "thumbnail_url": url, "video": _out(row, db, current_user.id)}


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
