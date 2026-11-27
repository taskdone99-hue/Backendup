"""Stable-ID upload lifecycle endpoints for separate Reel and Video records."""
import uuid
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session
from app import models
from app.auth import get_current_user
from app.database import get_db
from app.services.media_service import save_upload_file, get_video_metadata, generate_video_thumbnail, _local_path_from_url

router = APIRouter(tags=["Upload lifecycle"])
STATUSES = {"PENDING", "UPLOADING", "PROCESSING", "READY", "PUBLISHED", "FAILED"}


def _get(db, model, uid, user):
    row = db.query(model).filter(model.video_uid == uid).first()
    if row is None:
        raise HTTPException(404, "Upload not found")
    if row.user_id != user.id:
        raise HTTPException(403, "You can only manage your own uploads")
    return row


def _init(db, model, user, title=None):
    uid = uuid.uuid4().hex
    if model is models.Reel:
        row = model(user_id=user.id, video_uid=uid, upload_status="PENDING", upload_progress=0,
                    video_url=None, title=title, caption=None)
    else:
        row = model(user_id=user.id, video_uid=uid, upload_status="PENDING", upload_progress=0,
                    video_url=None, title=title, mime_type=None, duration_seconds=None, file_size=0)
    db.add(row); db.commit(); db.refresh(row)
    return {"message": "Upload initiated", "video_uid": uid, "record_id": row.id,
            "content_type": "reel" if model is models.Reel else "video",
            "upload_status": row.upload_status, "upload_progress": 0}


@router.post("/api/reels/initiate", status_code=201)
def initiate_reel(title: str | None = Form(None), db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _init(db, models.Reel, user, title)

@router.post("/api/videos/initiate", status_code=201)
def initiate_video(title: str | None = Form(None), db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _init(db, models.Video, user, title)


def _upload(db, model, uid, file, user, folder):
    row = _get(db, model, uid, user)
    row.upload_status = "UPLOADING"; row.upload_progress = 0
    db.commit()
    url, kind = save_upload_file(file, folder, allow_video=True)
    if kind != "video":
        raise HTTPException(400, "File must be a video")
    meta = get_video_metadata(url)
    if not meta:
        raise HTTPException(400, "Could not read video metadata; ensure ffprobe is installed")
    path = _local_path_from_url(url)
    row.video_url = url
    row.thumbnail_url = generate_video_thumbnail(url, subfolder="video_thumbnails")
    row.upload_progress = 100
    row.upload_status = "PROCESSING"
    if model is models.Reel:
        row.duration_seconds = meta["duration_seconds"]
    else:
        row.mime_type = file.content_type or "video/mp4"
        row.file_size = path.stat().st_size if path else 0
        row.duration_seconds = meta["duration_seconds"]
        row.width, row.height = meta["width"], meta["height"]
        row.aspect_ratio = f'{meta["width"]}:{meta["height"]}'
        row.orientation = "horizontal" if meta["width"] > meta["height"] else "vertical"
        if not row.title:
            row.title = (file.filename or "Untitled video")[:150]
    db.commit(); db.refresh(row)
    return {"message": "Upload received", "video_uid": uid, "record_id": row.id,
            "upload_status": row.upload_status, "upload_progress": row.upload_progress,
            "video_url": row.video_url, "thumbnail_url": row.thumbnail_url}

@router.put("/api/reels/{video_uid}/upload")
def upload_reel(video_uid: str, file: UploadFile = File(...), db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _upload(db, models.Reel, video_uid, file, user, "reels")

@router.put("/api/videos/{video_uid}/upload")
def upload_video(video_uid: str, file: UploadFile = File(...), db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _upload(db, models.Video, video_uid, file, user, "videos")


def _status(db, model, uid, user):
    row = _get(db, model, uid, user)
    return {"video_uid": row.video_uid, "record_id": row.id, "upload_status": row.upload_status,
            "upload_progress": row.upload_progress, "video_url": row.video_url,
            "thumbnail_url": row.thumbnail_url}

@router.get("/api/reels/{video_uid}/status")
def reel_status(video_uid: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _status(db, models.Reel, video_uid, user)

@router.get("/api/videos/{video_uid}/status")
def video_status(video_uid: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _status(db, models.Video, video_uid, user)


def _progress(db, model, uid, progress, user):
    row = _get(db, model, uid, user)
    if row.upload_status in {"READY", "PUBLISHED"}:
        raise HTTPException(409, "Upload is already complete")
    row.upload_progress = max(0, min(100, progress))
    row.upload_status = "UPLOADING" if row.upload_progress < 100 else "PROCESSING"
    db.commit()
    return _status(db, model, uid, user)

@router.put("/api/reels/{video_uid}/progress")
def reel_progress(video_uid: str, progress: int = Form(..., ge=0, le=100), db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _progress(db, models.Reel, video_uid, progress, user)

@router.put("/api/videos/{video_uid}/progress")
def video_progress(video_uid: str, progress: int = Form(..., ge=0, le=100), db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _progress(db, models.Video, video_uid, progress, user)


def _complete(db, model, uid, user):
    row = _get(db, model, uid, user)
    if not row.video_url:
        raise HTTPException(409, "Upload a video file before completing")
    row.upload_status = "READY"; row.upload_progress = 100
    db.commit()
    return _status(db, model, uid, user)

@router.post("/api/reels/{video_uid}/complete")
def complete_reel(video_uid: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _complete(db, models.Reel, video_uid, user)

@router.post("/api/videos/{video_uid}/complete")
def complete_video(video_uid: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _complete(db, models.Video, video_uid, user)


def _publish(db, model, uid, user):
    row = _get(db, model, uid, user)
    if row.upload_status != "READY":
        raise HTTPException(409, "Upload must be READY before publishing")
    row.upload_status = "PUBLISHED"; db.commit()
    return {"message": "Published successfully", **_status(db, model, uid, user)}

@router.post("/api/reels/{video_uid}/publish")
def publish_reel(video_uid: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _publish(db, models.Reel, video_uid, user)

@router.post("/api/videos/{video_uid}/publish")
def publish_video(video_uid: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _publish(db, models.Video, video_uid, user)


def _retry(db, model, uid, user):
    row = _get(db, model, uid, user)
    row.upload_status = "PENDING"; row.upload_progress = 0
    db.commit()
    return {"message": "Retry initialized; reuse this video_uid", **_status(db, model, uid, user)}

@router.post("/api/reels/{video_uid}/retry")
def retry_reel(video_uid: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _retry(db, models.Reel, video_uid, user)

@router.post("/api/videos/{video_uid}/retry")
def retry_video(video_uid: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _retry(db, models.Video, video_uid, user)
