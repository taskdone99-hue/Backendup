"""Pre-publish settings addressed by stable Reel/Video upload UIDs."""

import json

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db
from app.services.media_service import save_upload_file


router = APIRouter(tags=["UID upload settings"])

ALLOWED = {
    "audience": {"everyone", "followers", "close_friends"},
    "visibility": {"public", "private", "members"},
}


def _record(db: Session, model, uid: str, user):
    """Fetch an upload by its stored UID and enforce ownership/pre-publish state."""
    row = db.query(model).filter(model.video_uid == uid).first()

    if row is None:
        raise HTTPException(status_code=404, detail="Upload UID not found")

    if row.user_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="You can only manage your own uploads",
        )

    if row.upload_status == "PUBLISHED":
        raise HTTPException(
            status_code=409,
            detail="Upload is already published; use the published-ID settings API",
        )

    return row


def _get_settings(db: Session, kind: str, row):
    return (
        db.query(models.UploadUIDSettings)
        .filter_by(content_type=kind, content_id=row.id)
        .first()
    )


def _save(db: Session, kind: str, row, key: str, value):
    obj = _get_settings(db, kind, row)

    if obj is None:
        obj = models.UploadUIDSettings(
            content_type=kind,
            content_id=row.id,
            settings={},
        )
        db.add(obj)

    data = dict(obj.settings or {})
    data[key] = value
    obj.settings = data
    db.commit()
    db.refresh(obj)

    return data


def _settings_response(kind: str, uid: str, data: dict):
    uid_key = "reel_uid" if kind == "reel" else "video_uid"
    return {uid_key: uid, "settings": data}


def _uid_from_request(request: Request, uid_name: str) -> str:
    """Read the correctly named path parameter (reel_uid or video_uid)."""
    uid = request.path_params.get(uid_name)

    if not uid:
        raise HTTPException(status_code=422, detail=f"{uid_name} is required")

    return uid


def _register(kind: str, model, uid_name: str, base: str):
    def get_row(db: Session, uid: str, user):
        return _record(db, model, uid, user)

    @router.get(f"{base}/{{{uid_name}}}/editor-state")
    def get_editor_state(
        request: Request,
        db: Session = Depends(get_db),
        user=Depends(get_current_user),
    ):
        uid = _uid_from_request(request, uid_name)
        row = get_row(db, uid, user)
        return {
            uid_name: uid,
            "editor_state": row.editor_state,
        }

    @router.put(f"{base}/{{{uid_name}}}/editor-state")
    def save_editor_state(
        request: Request,
        payload: dict,
        db: Session = Depends(get_db),
        user=Depends(get_current_user),
    ):
        uid = _uid_from_request(request, uid_name)
        row = get_row(db, uid, user)
        if "editor_state" not in payload or not isinstance(payload["editor_state"], dict):
            raise HTTPException(
                status_code=422,
                detail="editor_state must be a JSON object",
            )
        try:
            encoded = json.dumps(payload["editor_state"], ensure_ascii=False)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="editor_state must contain valid JSON data")
        if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="editor_state cannot exceed 2 MB")

        row.editor_state = payload["editor_state"]
        db.commit()
        db.refresh(row)
        return {
            uid_name: uid,
            "message": "Editor state saved",
            "editor_state": row.editor_state,
        }

    @router.delete(f"{base}/{{{uid_name}}}/editor-state")
    def clear_editor_state(
        request: Request,
        db: Session = Depends(get_db),
        user=Depends(get_current_user),
    ):
        uid = _uid_from_request(request, uid_name)
        row = get_row(db, uid, user)
        row.editor_state = None
        db.commit()
        return {
            uid_name: uid,
            "message": "Editor state cleared",
            "editor_state": None,
        }

    @router.put(f"{base}/{{{uid_name}}}/audience")
    def set_audience(
        request: Request,
        payload: dict,
        db: Session = Depends(get_db),
        user=Depends(get_current_user),
    ):
        uid = _uid_from_request(request, uid_name)
        row = get_row(db, uid, user)

        audience = payload.get("audience")
        if audience not in ALLOWED["audience"]:
            raise HTTPException(
                status_code=422,
                detail="audience must be everyone, followers, or close_friends",
            )

        data = _save(db, kind, row, "audience", audience)

        if kind == "reel":
            row.audience = audience
            db.commit()

        return _settings_response(kind, uid, data)

    @router.put(f"{base}/{{{uid_name}}}/schedule")
    def set_schedule(
        request: Request,
        payload: dict,
        db: Session = Depends(get_db),
        user=Depends(get_current_user),
    ):
        uid = _uid_from_request(request, uid_name)
        row = get_row(db, uid, user)

        enabled = payload.get("schedule_enabled", True)
        scheduled_at = payload.get("scheduled_at")

        if not isinstance(enabled, bool):
            raise HTTPException(
                status_code=422,
                detail="schedule_enabled must be a boolean",
            )

        if enabled and not scheduled_at:
            raise HTTPException(
                status_code=422,
                detail="scheduled_at is required when schedule_enabled is true",
            )

        value = {
            "schedule_enabled": enabled,
            "scheduled_at": scheduled_at if enabled else None,
        }
        data = _save(db, kind, row, "schedule", value)

        if kind == "reel":
            row.schedule_enabled = enabled
            row.scheduled_at = scheduled_at if enabled else None
            db.commit()

        return _settings_response(kind, uid, data)

    @router.delete(f"{base}/{{{uid_name}}}/schedule")
    def clear_schedule(
        request: Request,
        db: Session = Depends(get_db),
        user=Depends(get_current_user),
    ):
        uid = _uid_from_request(request, uid_name)
        row = get_row(db, uid, user)

        value = {"schedule_enabled": False, "scheduled_at": None}
        data = _save(db, kind, row, "schedule", value)

        if kind == "reel":
            row.schedule_enabled = False
            row.scheduled_at = None
            db.commit()

        return _settings_response(kind, uid, data)

    @router.put(f"{base}/{{{uid_name}}}/comment-dm-automation")
    def set_comment_dm(
        request: Request,
        payload: dict,
        db: Session = Depends(get_db),
        user=Depends(get_current_user),
    ):
        uid = _uid_from_request(request, uid_name)
        row = get_row(db, uid, user)

        data = _save(db, kind, row, "comment_dm_automation", payload)
        return _settings_response(kind, uid, data)

    @router.get(f"{base}/{{{uid_name}}}/comment-dm-automation")
    def get_comment_dm(
        request: Request,
        db: Session = Depends(get_db),
        user=Depends(get_current_user),
    ):
        uid = _uid_from_request(request, uid_name)
        row = get_row(db, uid, user)

        obj = _get_settings(db, kind, row)
        value = (
            (obj.settings or {}).get("comment_dm_automation", {})
            if obj
            else {}
        )

        return _settings_response(kind, uid, value)

    @router.post(f"{base}/{{{uid_name}}}/collaborators")
    def add_collaborator(
        request: Request,
        payload: dict,
        db: Session = Depends(get_db),
        user=Depends(get_current_user),
    ):
        uid = _uid_from_request(request, uid_name)
        row = get_row(db, uid, user)

        collaborator_id = payload.get("user_id")
        if (
            not isinstance(collaborator_id, int)
            or isinstance(collaborator_id, bool)
            or collaborator_id <= 0
        ):
            raise HTTPException(
                status_code=422,
                detail="user_id must be a positive integer",
            )

        collaborator = (
            db.query(models.User)
            .filter(models.User.id == collaborator_id)
            .first()
        )
        if collaborator is None:
            raise HTTPException(
                status_code=404,
                detail="Collaborator user not found",
            )

        obj = _get_settings(db, kind, row)
        current = (
            list((obj.settings or {}).get("collaborators", []))
            if obj
            else []
        )

        if collaborator_id not in current:
            current.append(collaborator_id)

        data = _save(db, kind, row, "collaborators", current)
        return _settings_response(kind, uid, data)

    @router.put(f"{base}/{{{uid_name}}}/engagement-settings")
    def set_engagement(
        request: Request,
        payload: dict,
        db: Session = Depends(get_db),
        user=Depends(get_current_user),
    ):
        uid = _uid_from_request(request, uid_name)
        row = get_row(db, uid, user)

        allowed = {
            "comments_enabled",
            "hide_like_count",
            "hide_comments",
            "remixing_enabled",
            "downloads_enabled",
        }

        if (
            not payload
            or set(payload) - allowed
            or any(not isinstance(value, bool) for value in payload.values())
        ):
            raise HTTPException(
                status_code=422,
                detail="Provide boolean engagement settings using supported fields",
            )

        data = _save(db, kind, row, "engagement_settings", payload)

        if kind == "reel":
            for key, value in payload.items():
                setattr(row, key, value)
            db.commit()

        return _settings_response(kind, uid, data)

    @router.put(f"{base}/{{{uid_name}}}/language")
    def set_language(
        request: Request,
        payload: dict,
        db: Session = Depends(get_db),
        user=Depends(get_current_user),
    ):
        uid = _uid_from_request(request, uid_name)
        row = get_row(db, uid, user)

        language = payload.get("language")
        if (
            not isinstance(language, str)
            or not language.strip()
            or len(language.strip()) > 50
        ):
            raise HTTPException(
                status_code=422,
                detail="language is required and must be at most 50 characters",
            )

        language = language.strip()
        data = _save(db, kind, row, "language", language)

        if kind == "reel":
            row.language = language
            db.commit()

        return _settings_response(kind, uid, data)

    @router.post(f"{base}/{{{uid_name}}}/thumbnail")
    def upload_cover(
        request: Request,
        file: UploadFile = File(...),
        db: Session = Depends(get_db),
        user=Depends(get_current_user),
    ):
        uid = _uid_from_request(request, uid_name)
        row = get_row(db, uid, user)

        url, media_kind = save_upload_file(
            file,
            "video_thumbnails",
            allow_video=False,
        )

        if media_kind != "image":
            raise HTTPException(
                status_code=400,
                detail="Cover must be an image",
            )

        row.thumbnail_url = url
        data = _save(db, kind, row, "thumbnail_url", url)
        db.commit()

        return {
            **_settings_response(kind, uid, data),
            "thumbnail_url": url,
        }


_register("reel", models.Reel, "reel_uid", "/api/reels")
_register("video", models.Video, "video_uid", "/api/videos/uid")
