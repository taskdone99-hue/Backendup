"""
"Video" endpoints layered on top of the Reel model — this app has a single
video-content type (Reel: video_url + optional thumbnail), so /api/videos
manages the same `reels` rows that POST /api/reels creates, rather than
introducing a second, parallel entity.

Typical creation flow:

  1. POST /api/videos/upload            -> creates the reel, returns it
  2. POST /api/videos/{id}/thumbnail    -> attaches a thumbnail image
  3. PUT  /api/videos/{id}/metadata     -> sets title + description
  4. POST /api/videos/{id}/collaborators -> tags co-creators
  5. PUT  /api/videos/{id}/revenue-split -> splits ad revenue among creator + collaborators

Upload-page settings (each owner-only, each returns the saved value plus the
full updated video):

  PUT    /api/videos/{id}/language             -> spoken language
  PUT    /api/videos/{id}/ai-content           -> "AI-generated content" label
  PUT    /api/videos/{id}/engagement-settings  -> comments on/off, hide likes, hide comments
  PUT    /api/videos/{id}/audience             -> 18_plus | below_18
  PUT    /api/videos/{id}/visibility           -> public | private | members
  PUT    /api/videos/{id}/schedule             -> publish at a future UTC time
  DELETE /api/videos/{id}/schedule             -> cancel the schedule (goes live now)

How the settings are enforced is documented in
app/services/video_settings_service.py.

Storage: files are saved to local disk via app.services.media_service (see
that module's docstring for how to swap it for a real S3 `put_object` call
in production — nothing in the routes below needs to change either way).
"""

from pathlib import Path
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import get_current_user
from app.database import get_db
from app.services.media_service import generate_video_thumbnail, save_upload_file, STATIC_ROOT, _public_url
from app.services.location_service import find_or_create_location
from app.services import editor_state_service, video_settings_service as vss

router = APIRouter(prefix="/api/videos", tags=["videos"])


def _get_owned_video_or_404(
    db: Session, video_id: int, current_user: models.User
) -> models.Reel:
    video = db.query(models.Reel).filter(models.Reel.id == video_id).first()
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
    if video.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only manage your own videos"
        )
    return video


from app.routers.content_routes import _to_reel_detail


@router.post("/upload", response_model=schemas.ReelDetailOut, status_code=status.HTTP_201_CREATED)
def upload_video(
    file: UploadFile,
    title: str | None = Form(default=None),
    caption: str | None = Form(default=None),
    editor_state: str | None = Form(
        default=None, description=editor_state_service.EDITOR_STATE_FORM_DESCRIPTION
    ),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    parsed_editor_state = editor_state_service.parse_form_value(editor_state)  # before the file is written
    url, kind = save_upload_file(file, "reels", allow_video=True)
    if kind != "video":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File must be a video")

    thumbnail_url = generate_video_thumbnail(url)

    video = models.Reel(
        user_id=current_user.id, title=title, caption=caption, video_url=url,
        thumbnail_url=thumbnail_url, editor_state=parsed_editor_state,
    )
    db.add(video)
    db.commit()
    db.refresh(video)
    return _to_reel_detail(db, video, current_user.id)


@router.post("/{video_id}/thumbnail", response_model=schemas.ThumbnailUploadResponse)
def upload_thumbnail(
    video_id: int,
    file: UploadFile,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    video = _get_owned_video_or_404(db, video_id, current_user)
    url, _kind = save_upload_file(file, "thumbnails", allow_video=False)
    video.thumbnail_url = url
    db.commit()
    return schemas.ThumbnailUploadResponse(message="Thumbnail updated", thumbnail_url=url)


@router.put("/{video_id}/metadata", response_model=schemas.ReelDetailOut)
def update_video_metadata(
    video_id: int,
    payload: schemas.VideoMetadataUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    video = _get_owned_video_or_404(db, video_id, current_user)
    updates = payload.model_dump(exclude_unset=True)
    if "title" in updates:
        video.title = updates["title"]
    if "description" in updates:
        video.caption = updates["description"]

    if "editor_state" in updates:
        video.editor_state = editor_state_service.check_object(updates["editor_state"])

    if "location" in updates:
        location = updates["location"]
        if location is None:
            video.location_name = None
            video.location_latitude = None
            video.location_longitude = None
            video.location_id = None
        else:
            loc_row = find_or_create_location(
                db,
                name=location["name"],
                latitude=location.get("latitude"),
                longitude=location.get("longitude"),
            )
            video.location_name = loc_row.name
            video.location_latitude = loc_row.latitude
            video.location_longitude = loc_row.longitude
            video.location_id = loc_row.id

    db.commit()
    db.refresh(video)
    return _to_reel_detail(db, video, current_user.id)


@router.post(
    "/{video_id}/collaborators",
    response_model=schemas.CollaboratorsResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_collaborator(
    video_id: int,
    payload: schemas.CollaboratorAddRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    video = _get_owned_video_or_404(db, video_id, current_user)

    collaborator_user = db.query(models.User).filter(models.User.id == payload.user_id).first()
    if collaborator_user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if collaborator_user.id == video.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The creator doesn't need to be added as a collaborator",
        )

    existing = (
        db.query(models.ReelCollaborator)
        .filter(
            models.ReelCollaborator.reel_id == video_id,
            models.ReelCollaborator.user_id == payload.user_id,
        )
        .first()
    )
    if existing is None:
        db.add(models.ReelCollaborator(reel_id=video_id, user_id=payload.user_id))
        db.commit()

    collaborators = (
        db.query(models.ReelCollaborator)
        .filter(models.ReelCollaborator.reel_id == video_id)
        .all()
    )
    return schemas.CollaboratorsResponse(message="Collaborator added", collaborators=collaborators)


@router.put("/{video_id}/revenue-split", response_model=schemas.RevenueSplitResponse)
def update_revenue_split(
    video_id: int,
    payload: schemas.RevenueSplitUpdateRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Replaces the video's entire revenue split in one call. Every entry must
    be the video's creator or an already-tagged collaborator (add them via
    POST /api/videos/:id/collaborators first) — this stops revenue being
    routed to someone with no credited involvement in the video.
    """
    video = _get_owned_video_or_404(db, video_id, current_user)

    collaborator_ids = {
        row[0]
        for row in db.query(models.ReelCollaborator.user_id)
        .filter(models.ReelCollaborator.reel_id == video_id)
        .all()
    }
    allowed_ids = collaborator_ids | {video.user_id}
    for entry in payload.splits:
        if entry.user_id not in allowed_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"User {entry.user_id} must be the creator or a tagged "
                    "collaborator before getting a revenue share"
                ),
            )

    db.query(models.ReelRevenueShare).filter(
        models.ReelRevenueShare.reel_id == video_id
    ).delete(synchronize_session=False)
    for entry in payload.splits:
        db.add(
            models.ReelRevenueShare(
                reel_id=video_id, user_id=entry.user_id, percentage=entry.percentage
            )
        )
    db.commit()

    splits = (
        db.query(models.ReelRevenueShare)
        .filter(models.ReelRevenueShare.reel_id == video_id)
        .all()
    )
    return schemas.RevenueSplitResponse(
        message="Revenue split updated",
        splits=[
            schemas.RevenueShareOut(user_id=s.user_id, percentage=s.percentage) for s in splits
        ],
    )


# ==========================================================================
# Upload-page settings
# ==========================================================================

def _detail(db: Session, video: models.Reel, current_user: models.User) -> schemas.ReelDetailOut:
    db.refresh(video)
    return _to_reel_detail(db, video, current_user.id)


@router.put(
    "/{video_id}/language",
    response_model=schemas.VideoLanguageResponse,
    summary="Set the video's language",
)
def update_video_language(
    video_id: int,
    payload: schemas.VideoLanguageUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Saves the language chosen on the upload page. Accepts a language name
    (`English`, `Hindi`, `Telugu`...) or its ISO 639-1 code (`en`, `hi`, `te`),
    in any letter case, and stores/returns the canonical name. An empty or
    unsupported value is a 400. Owner only (403 otherwise); every other field
    of the video is left as it was.
    """
    video = _get_owned_video_or_404(db, video_id, current_user)
    video.language = payload.language
    db.commit()
    return schemas.VideoLanguageResponse(
        message="Language updated",
        video_id=video.id,
        language=video.language,
        video=_detail(db, video, current_user),
    )


@router.put(
    "/{video_id}/ai-content",
    response_model=schemas.VideoAIContentResponse,
    summary="Turn the 'AI-generated content' label on or off",
)
def update_video_ai_content(
    video_id: int,
    payload: schemas.VideoAIContentUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Stores the upload page's **AI-Generated Content** switch. Videos that
    predate this setting read as `ai_generated: false`. Owner only.
    """
    video = _get_owned_video_or_404(db, video_id, current_user)
    video.ai_generated = payload.ai_generated
    db.commit()
    return schemas.VideoAIContentResponse(
        message="AI-generated content setting updated",
        video_id=video.id,
        ai_generated=video.ai_generated,
        video=_detail(db, video, current_user),
    )


@router.put(
    "/{video_id}/engagement-settings",
    response_model=schemas.VideoEngagementSettingsResponse,
    summary="Comments on/off, hide like count, hide comments",
)
def update_video_engagement_settings(
    video_id: int,
    payload: schemas.VideoEngagementSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Saves the three engagement switches in one call (send all three, or only
    the ones you want to change). Owner only. Effects on the existing APIs:

    - `comments_enabled: false` — `POST /api/reels/{id}/comments` and
      `POST /api/comments/{id}/reply` return 403 for everyone, owner included.
      Existing comments are kept.
    - `hide_like_count: true` — everyone except the owner gets
      `likes_count: null` on the reel, `likes_count: null` from
      `POST /api/likes`, and 403 from `GET /api/reels/{id}/likes`.
    - `hide_comments: true` — everyone except the owner gets 403 from
      `GET /api/reels/{id}/comments` and `GET /api/comments/{id}/replies`.
      (`comments_count` is still returned.)
    """
    video = _get_owned_video_or_404(db, video_id, current_user)
    updates = payload.model_dump(exclude_unset=True)
    for field in ("comments_enabled", "hide_like_count", "hide_comments"):
        if field in updates:
            setattr(video, field, updates[field])
    db.commit()
    return schemas.VideoEngagementSettingsResponse(
        message="Engagement settings updated",
        video_id=video.id,
        comments_enabled=video.comments_enabled,
        hide_like_count=video.hide_like_count,
        hide_comments=video.hide_comments,
        video=_detail(db, video, current_user),
    )


@router.put(
    "/{video_id}/audience",
    response_model=schemas.VideoAudienceResponse,
    summary="Set the video's audience (18_plus or below_18)",
)
def update_video_audience(
    video_id: int,
    payload: schemas.VideoAudienceUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Stores the audience label: exactly `18_plus` or `below_18` (anything else
    is a 400). It is stored and returned only — it does not yet restrict who
    can watch. Videos that predate it read as `audience: null`. Owner only.
    """
    video = _get_owned_video_or_404(db, video_id, current_user)
    video.audience = payload.audience.value
    db.commit()
    return schemas.VideoAudienceResponse(
        message="Audience updated",
        video_id=video.id,
        audience=video.audience,
        video=_detail(db, video, current_user),
    )


@router.put(
    "/{video_id}/visibility",
    response_model=schemas.VideoVisibilityResponse,
    summary="Set who can see the video (public, private, members)",
)
def update_video_visibility(
    video_id: int,
    payload: schemas.VideoVisibilityUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    - `public` — anyone who can see the owner's content (existing block /
      private-account rules still apply).
    - `private` — only the owner and credited collaborators. Everyone else
      gets 404 from the reel detail/comments/likes/share/save endpoints and
      the reel disappears from feeds, profile grids, audio/location/tag
      lists and the public `/r/{id}` page.
    - `members` — **stored, but no creator-membership system exists yet**
      (`/api/membership` is a platform subscription, not "member of creator
      X"). Until one is added, a members-only video behaves like `private`.

    Owner only.
    """
    video = _get_owned_video_or_404(db, video_id, current_user)
    video.visibility = payload.visibility.value
    db.commit()
    return schemas.VideoVisibilityResponse(
        message="Visibility updated",
        video_id=video.id,
        visibility=video.visibility,
        video=_detail(db, video, current_user),
    )


@router.put(
    "/{video_id}/schedule",
    response_model=schemas.VideoScheduleResponse,
    summary="Schedule the video to go live at a future time",
)
def update_video_schedule(
    video_id: int,
    payload: schemas.VideoScheduleUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    `schedule_enabled: true` needs `scheduled_at` (UTC; a value without a
    timezone offset is read as UTC) and it must be in the future (400
    otherwise). Until that moment the video is visible only to its owner and
    collaborators; at that moment it becomes visible according to its
    `visibility`. Nothing runs in a background task — the check happens on
    every read, so it survives server restarts.

    `schedule_enabled: false` clears the schedule. Changing a schedule that
    has already fired (the video went live on schedule) is a 409.
    Owner only.
    """
    video = _get_owned_video_or_404(db, video_id, current_user)

    if not payload.schedule_enabled:
        video.schedule_enabled = False
        video.scheduled_at = None
    else:
        now = vss.utcnow()
        if vss.is_schedule_elapsed(video, now):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This video was already published on its schedule; the schedule can't be changed",
            )
        try:
            in_the_past = vss.as_utc(payload.scheduled_at) <= now
            stored = vss.to_storage_utc(payload.scheduled_at)
        except (OverflowError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="scheduled_at is out of range"
            )
        if in_the_past:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="scheduled_at must be in the future",
            )
        video.schedule_enabled = True
        video.scheduled_at = stored

    db.commit()
    return schemas.VideoScheduleResponse(
        message="Schedule updated" if video.schedule_enabled else "Schedule cleared",
        video_id=video.id,
        schedule_enabled=video.schedule_enabled,
        scheduled_at=vss.as_utc(video.scheduled_at),
        video=_detail(db, video, current_user),
    )


@router.delete(
    "/{video_id}/schedule",
    response_model=schemas.VideoScheduleResponse,
    summary="Cancel the schedule",
)
def delete_video_schedule(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Removes the schedule. A video that was still waiting for its time goes
    live immediately (subject to `visibility`); one that already went live is
    unaffected. Idempotent. Owner only.
    """
    video = _get_owned_video_or_404(db, video_id, current_user)
    video.schedule_enabled = False
    video.scheduled_at = None
    db.commit()
    return schemas.VideoScheduleResponse(
        message="Schedule cleared",
        video_id=video.id,
        schedule_enabled=False,
        scheduled_at=None,
        video=_detail(db, video, current_user),
    )


# ==========================================================================
# Additional Krizil upload-page workflows: captions, drafts, comment-to-DM
# ==========================================================================

def _get_owned_draft_or_404(db: Session, draft_id: int, current_user: models.User) -> models.ReelDraft:
    draft = db.query(models.ReelDraft).filter(models.ReelDraft.id == draft_id).first()
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only manage your own drafts")
    return draft


@router.post("/{video_id}/captions", response_model=schemas.ReelCaptionTrackOut, status_code=status.HTTP_201_CREATED)
def upload_reel_captions(
    video_id: int,
    file: UploadFile,
    language: str = Form(..., min_length=2, max_length=50),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Upload an SRT or VTT subtitle track. One track per language; re-upload replaces it."""
    video = _get_owned_video_or_404(db, video_id, current_user)
    filename = (file.filename or "").lower()
    extension = Path(filename).suffix
    if extension not in {".srt", ".vtt"}:
        raise HTTPException(status_code=400, detail="Caption file must use .srt or .vtt format")
    raw = file.file.read(5 * 1024 * 1024 + 1)
    if not raw:
        raise HTTPException(status_code=400, detail="Caption file is empty")
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Caption file exceeds 5 MB")
    try:
        raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Caption file must be UTF-8 text")
    if extension == ".vtt" and not raw.decode("utf-8-sig").lstrip().startswith("WEBVTT"):
        raise HTTPException(status_code=400, detail="VTT files must start with WEBVTT")
    canonical_language = language.strip()
    if not canonical_language:
        raise HTTPException(status_code=400, detail="language is required")
    folder = STATIC_ROOT / "captions"
    folder.mkdir(parents=True, exist_ok=True)
    filename_out = f"{uuid.uuid4().hex}{extension}"
    (folder / filename_out).write_bytes(raw)
    url = _public_url(f"captions/{filename_out}")
    track = db.query(models.ReelCaptionTrack).filter(
        models.ReelCaptionTrack.reel_id == video.id,
        models.ReelCaptionTrack.language == canonical_language,
    ).first()
    if track is None:
        track = models.ReelCaptionTrack(reel_id=video.id, language=canonical_language, format=extension[1:], file_url=url)
        db.add(track)
    else:
        old_url = track.file_url
        track.format = extension[1:]
        track.file_url = url
        try:
            old_path = STATIC_ROOT / old_url.split("/static/", 1)[-1] if "/static/" in old_url else None
            if old_path:
                old_path.unlink(missing_ok=True)
        except OSError:
            pass
    db.commit()
    db.refresh(track)
    return track


@router.get("/{video_id}/captions", response_model=schemas.ReelCaptionTracksResponse)
def list_reel_captions(
    video_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user),
):
    _get_owned_video_or_404(db, video_id, current_user)
    items = db.query(models.ReelCaptionTrack).filter(models.ReelCaptionTrack.reel_id == video_id).order_by(models.ReelCaptionTrack.language.asc()).all()
    return schemas.ReelCaptionTracksResponse(items=items)


@router.delete("/{video_id}/captions/{caption_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_reel_captions(
    video_id: int, caption_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user),
):
    _get_owned_video_or_404(db, video_id, current_user)
    track = db.query(models.ReelCaptionTrack).filter(models.ReelCaptionTrack.id == caption_id, models.ReelCaptionTrack.reel_id == video_id).first()
    if track is None:
        raise HTTPException(status_code=404, detail="Caption track not found")
    url = track.file_url
    db.delete(track)
    db.commit()
    try:
        if "/static/" in url:
            (STATIC_ROOT / url.split("/static/", 1)[-1]).unlink(missing_ok=True)
    except OSError:
        pass
    return None


@router.post("/drafts/upload", response_model=schemas.ReelDraftOut, status_code=status.HTTP_201_CREATED)
def create_reel_draft(
    file: UploadFile,
    title: str | None = Form(default=None, max_length=150),
    description: str | None = Form(default=None, max_length=2200),
    language: str | None = Form(default=None, max_length=50),
    ai_generated: bool = Form(default=False),
    audience: str | None = Form(default=None),
    visibility: str = Form(default="private"),
    comments_enabled: bool = Form(default=True),
    hide_like_count: bool = Form(default=False),
    hide_comments: bool = Form(default=False),
    editor_state: str | None = Form(default=None, description=editor_state_service.EDITOR_STATE_FORM_DESCRIPTION),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    parsed_editor_state = editor_state_service.parse_form_value(editor_state)
    if audience not in {None, "18_plus", "below_18"}:
        raise HTTPException(status_code=400, detail="audience must be 18_plus or below_18")
    if visibility not in {"public", "private", "members"}:
        raise HTTPException(status_code=400, detail="visibility must be public, private or members")
    video_url, kind = save_upload_file(file, "reels", allow_video=True)
    if kind != "video":
        raise HTTPException(status_code=400, detail="File must be a video")
    draft = models.ReelDraft(
        user_id=current_user.id, video_url=video_url, thumbnail_url=generate_video_thumbnail(video_url),
        title=title, caption=description, language=language, ai_generated=ai_generated, audience=audience,
        visibility=visibility, comments_enabled=comments_enabled, hide_like_count=hide_like_count,
        hide_comments=hide_comments, editor_state=parsed_editor_state,
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return draft


@router.get("/drafts", response_model=list[schemas.ReelDraftOut])
def list_reel_drafts(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    return db.query(models.ReelDraft).filter(models.ReelDraft.user_id == current_user.id).order_by(models.ReelDraft.updated_at.desc(), models.ReelDraft.id.desc()).all()


@router.put("/drafts/{draft_id}", response_model=schemas.ReelDraftOut)
def update_reel_draft(
    draft_id: int, payload: schemas.ReelDraftUpdate, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user),
):
    draft = _get_owned_draft_or_404(db, draft_id, current_user)
    values = payload.model_dump(exclude_unset=True)
    for key, value in values.items():
        target = "caption" if key == "description" else key
        if target == "audience" and value is not None:
            value = value.value
        if target == "visibility" and value is not None:
            value = value.value
        if target == "editor_state":
            value = editor_state_service.check_object(value)
        setattr(draft, target, value)
    db.commit()
    db.refresh(draft)
    return draft


@router.delete("/drafts/{draft_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_reel_draft(draft_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    draft = _get_owned_draft_or_404(db, draft_id, current_user)
    db.delete(draft)
    db.commit()
    return None


@router.post("/drafts/{draft_id}/publish", response_model=schemas.ReelDraftPublishResponse, status_code=status.HTTP_201_CREATED)
def publish_reel_draft(draft_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    draft = _get_owned_draft_or_404(db, draft_id, current_user)
    if not draft.video_url:
        raise HTTPException(status_code=400, detail="A video is required before publishing")
    reel = models.Reel(
        user_id=current_user.id, video_url=draft.video_url, thumbnail_url=draft.thumbnail_url, title=draft.title, caption=draft.caption,
        language=draft.language, ai_generated=draft.ai_generated, audience=draft.audience, visibility=draft.visibility,
        comments_enabled=draft.comments_enabled, hide_like_count=draft.hide_like_count, hide_comments=draft.hide_comments, editor_state=draft.editor_state,
    )
    db.add(reel)
    db.flush()
    draft_id_value = draft.id
    db.delete(draft)
    db.commit()
    db.refresh(reel)
    return schemas.ReelDraftPublishResponse(message="Draft published", draft_id=draft_id_value, reel=_to_reel_detail(db, reel, current_user.id))


def _owned_automation_video(db: Session, video_id: int, current_user: models.User) -> models.Reel:
    return _get_owned_video_or_404(db, video_id, current_user)


@router.put("/{video_id}/comment-dm-automation", response_model=schemas.CommentDMAutomationOut)
def upsert_comment_dm_automation(
    video_id: int, payload: schemas.CommentDMAutomationUpdate, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user),
):
    _owned_automation_video(db, video_id, current_user)
    automation = db.query(models.ReelCommentDMAutomation).filter(models.ReelCommentDMAutomation.reel_id == video_id).first()
    if automation is None:
        automation = models.ReelCommentDMAutomation(reel_id=video_id)
        db.add(automation)
    for key, value in payload.model_dump().items():
        setattr(automation, key, value)
    db.commit()
    db.refresh(automation)
    return automation


@router.get("/{video_id}/comment-dm-automation", response_model=schemas.CommentDMAutomationOut | None)
def get_comment_dm_automation(
    video_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user),
):
    _owned_automation_video(db, video_id, current_user)
    return db.query(models.ReelCommentDMAutomation).filter(models.ReelCommentDMAutomation.reel_id == video_id).first()
