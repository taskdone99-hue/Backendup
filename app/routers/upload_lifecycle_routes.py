
"""Stable-ID upload lifecycle endpoints for separate Reel and Video records."""
import json
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db
from app.services.media_service import (
    save_upload_file,
    get_video_metadata,
    generate_video_thumbnail,
    _local_path_from_url,
)

router = APIRouter(tags=["Upload lifecycle"])


def _uid_key(model):
    return "reel_uid" if model is models.Reel else "video_uid"


def _get(db, model, uid, user):
    row = db.query(model).filter(model.video_uid == uid).first()

    if row is None:
        raise HTTPException(404, "Upload not found")

    if row.user_id != user.id:
        raise HTTPException(403, "You can only manage your own uploads")

    return row


def _uid_response(model, uid):
    return {_uid_key(model): uid}


def _parse_form_list(value, field_name, *, lower=False, numeric=False):
    """Accept JSON arrays or comma-separated values from multipart form fields."""
    if value is None or value == "":
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        parsed = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(parsed, list):
        raise HTTPException(422, f"{field_name} must be a JSON array or comma-separated list")
    result = []
    seen = set()
    for item in parsed:
        if numeric:
            try:
                item = int(item)
            except (TypeError, ValueError):
                raise HTTPException(422, f"{field_name} must contain user IDs")
            if item <= 0:
                raise HTTPException(422, f"{field_name} must contain positive user IDs")
        else:
            item = str(item).strip().lstrip("#")
            if not item:
                continue
            if lower:
                item = item.lower()
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _init(
    db, model, user, title=None, caption=None, location=None,
    tagged_people=None, hashtags=None, age_restriction=None,
):
    tagged_ids = _parse_form_list(tagged_people, "tagged_people", numeric=True)
    hashtag_names = _parse_form_list(hashtags, "hashtags", lower=True)

    if len(hashtag_names) > 30:
        raise HTTPException(422, "A maximum of 30 hashtags is allowed")
    if age_restriction not in (None, "", "18_plus", "below_18"):
        raise HTTPException(422, "age_restriction must be '18_plus' or 'below_18'")

    if tagged_ids:
        found_ids = {
            row[0] for row in db.query(models.User.id).filter(models.User.id.in_(tagged_ids)).all()
        }
        missing = sorted(set(tagged_ids) - found_ids)
        if missing:
            raise HTTPException(422, f"Unknown tagged_people user IDs: {missing}")

    uid = uuid.uuid4().hex
    common = dict(
        user_id=user.id,
        video_uid=uid,
        upload_status="PENDING",
        upload_progress=0,
        video_url=None,
        title=title,
        caption=caption,
        age_restriction=age_restriction or None,
    )

    if model is models.Reel:
        row = model(
            **common,
            location_name=location,
            hashtags=hashtag_names,
        )
    else:
        # Video uses `description` for caption text and `location` for its
        # upload location; retain the existing schema and field names.
        common.pop("caption")
        common.pop("age_restriction")
        row = model(
            **common,
            description=caption,
            location=location,
            location_name=location,
            tags=hashtag_names,
            tag_user_ids=tagged_ids,
            age_restriction=age_restriction or None,
            mime_type=None,
            duration_seconds=None,
            file_size=0,
        )

    db.add(row)
    db.flush()

    # Reel tags are represented by the existing ReelTag association table.
    # Video tags use its existing tag_user_ids JSON column.
    if model is models.Reel:
        for tagged_id in tagged_ids:
            db.add(models.ReelTag(reel_id=row.id, user_id=tagged_id))

    db.commit()
    db.refresh(row)

    response = {
        "message": "Upload initiated",
        **_uid_response(model, uid),
        "record_id": row.id,
        "content_type": "reel" if model is models.Reel else "video",
        "upload_status": row.upload_status,
        "upload_progress": 0,
        "title": row.title,
        "caption": row.caption if model is models.Reel else row.description,
        "location": row.location_name if model is models.Reel else row.location,
        "tagged_people": tagged_ids,
        "hashtags": hashtag_names,
        "age_restriction": row.age_restriction,
    }
    return response


@router.post("/api/reels/initiate", status_code=201)
def initiate_reel(
    title: str | None = Form(None),
    caption: str | None = Form(None),
    location: str | None = Form(None),
    tagged_people: str | None = Form(None),
    hashtags: str | None = Form(None),
    age_restriction: str | None = Form(None),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    return _init(
        db, models.Reel, user, title, caption, location,
        tagged_people, hashtags, age_restriction,
    )


@router.post("/api/videos/initiate", status_code=201)
def initiate_video(
    title: str | None = Form(None),
    caption: str | None = Form(None),
    location: str | None = Form(None),
    tagged_people: str | None = Form(None),
    hashtags: str | None = Form(None),
    age_restriction: str | None = Form(None),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    return _init(
        db, models.Video, user, title, caption, location,
        tagged_people, hashtags, age_restriction,
    )


def _upload(db, model, uid, file, user, folder):
    row = _get(db, model, uid, user)

    row.upload_status = "UPLOADING"
    row.upload_progress = 0
    db.commit()

    url, kind = save_upload_file(file, folder, allow_video=True)

    if kind != "video":
        raise HTTPException(400, "File must be a video")

    meta = get_video_metadata(url)

    if not meta:
        raise HTTPException(
            400,
            "Could not read video metadata; ensure ffprobe is installed",
        )

    path = _local_path_from_url(url)

    row.video_url = url
    row.thumbnail_url = generate_video_thumbnail(
        url,
        subfolder="video_thumbnails",
    )
    row.upload_progress = 100
    row.upload_status = "PROCESSING"

    if model is models.Reel:
        row.duration_seconds = meta["duration_seconds"]
    else:
        row.mime_type = file.content_type or "video/mp4"
        row.file_size = path.stat().st_size if path else 0
        row.duration_seconds = meta["duration_seconds"]
        row.width = meta["width"]
        row.height = meta["height"]
        row.aspect_ratio = f'{meta["width"]}:{meta["height"]}'
        row.orientation = (
            "horizontal"
            if meta["width"] > meta["height"]
            else "vertical"
        )

        if not row.title:
            row.title = (file.filename or "Untitled video")[:150]

    db.commit()
    db.refresh(row)

    return {
        "message": "Upload received",
        **_uid_response(model, uid),
        "record_id": row.id,
        "upload_status": row.upload_status,
        "upload_progress": row.upload_progress,
        "video_url": row.video_url,
        "thumbnail_url": row.thumbnail_url,
    }


@router.put("/api/reels/{reel_uid}/upload")
def upload_reel(
    reel_uid: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    return _upload(db, models.Reel, reel_uid, file, user, "reels")


@router.put("/api/videos/{video_uid}/upload")
def upload_video(
    video_uid: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    return _upload(db, models.Video, video_uid, file, user, "videos")


def _status(db, model, uid, user):
    row = _get(db, model, uid, user)

    return {
        **_uid_response(model, row.video_uid),
        "record_id": row.id,
        "upload_status": row.upload_status,
        "upload_progress": row.upload_progress,
        "video_url": row.video_url,
        "thumbnail_url": row.thumbnail_url,
    }


@router.get("/api/reels/{reel_uid}/status")
def reel_status(
    reel_uid: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    return _status(db, models.Reel, reel_uid, user)


@router.get("/api/videos/{video_uid}/status")
def video_status(
    video_uid: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    return _status(db, models.Video, video_uid, user)


def _progress(db, model, uid, progress, user):
    row = _get(db, model, uid, user)

    if row.upload_status in {"READY", "PUBLISHED"}:
        raise HTTPException(409, "Upload is already complete")

    row.upload_progress = max(0, min(100, progress))
    row.upload_status = (
        "UPLOADING" if row.upload_progress < 100 else "PROCESSING"
    )

    db.commit()

    return _status(db, model, uid, user)


@router.put("/api/reels/{reel_uid}/progress")
def reel_progress(
    reel_uid: str,
    progress: int = Form(..., ge=0, le=100),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    return _progress(db, models.Reel, reel_uid, progress, user)


@router.put("/api/videos/{video_uid}/progress")
def video_progress(
    video_uid: str,
    progress: int = Form(..., ge=0, le=100),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    return _progress(db, models.Video, video_uid, progress, user)


def _complete(db, model, uid, user):
    row = _get(db, model, uid, user)

    if not row.video_url:
        raise HTTPException(
            409,
            "Upload a video file before completing",
        )

    row.upload_status = "READY"
    row.upload_progress = 100

    db.commit()

    return _status(db, model, uid, user)


@router.post("/api/reels/{reel_uid}/complete")
def complete_reel(
    reel_uid: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    return _complete(db, models.Reel, reel_uid, user)


@router.post("/api/videos/{video_uid}/complete")
def complete_video(
    video_uid: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    return _complete(db, models.Video, video_uid, user)


def _publish(db, model, uid, user):
    row = _get(db, model, uid, user)

    if row.upload_status != "READY":
        raise HTTPException(
            409,
            "Upload must be READY before publishing",
        )

    row.upload_status = "PUBLISHED"
    db.commit()

    return {
        "message": "Published successfully",
        **_status(db, model, uid, user),
    }


@router.post("/api/reels/{reel_uid}/publish")
def publish_reel(
    reel_uid: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    return _publish(db, models.Reel, reel_uid, user)


@router.post("/api/videos/{video_uid}/publish")
def publish_video(
    video_uid: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    return _publish(db, models.Video, video_uid, user)


def _retry(db, model, uid, user):
    row = _get(db, model, uid, user)

    row.upload_status = "PENDING"
    row.upload_progress = 0

    db.commit()

    return {
        "message": "Retry initialized",
        **_status(db, model, uid, user),
    }


@router.post("/api/reels/{reel_uid}/retry")
def retry_reel(
    reel_uid: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    return _retry(db, models.Reel, reel_uid, user)


@router.post("/api/videos/{video_uid}/retry")
def retry_video(
    video_uid: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    return _retry(db, models.Video, video_uid, user)
