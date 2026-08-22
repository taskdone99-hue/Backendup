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

Storage: files are saved to local disk via app.services.media_service (see
that module's docstring for how to swap it for a real S3 `put_object` call
in production — nothing in the routes below needs to change either way).
"""

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import get_current_user
from app.database import get_db
from app.services import engagement
from app.services.media_service import generate_video_thumbnail, save_upload_file

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


def _to_reel_detail(
    db: Session, reel: models.Reel, viewer_id: int | None
) -> schemas.ReelDetailOut:
    detail = schemas.ReelDetailOut.model_validate(reel)
    detail.likes_count = engagement.likes_count(db, models.LikeTargetType.reel, reel.id)
    detail.is_liked = engagement.is_liked_by(
        db, viewer_id, models.LikeTargetType.reel, reel.id
    )
    return detail


@router.post("/upload", response_model=schemas.ReelDetailOut, status_code=status.HTTP_201_CREATED)
def upload_video(
    file: UploadFile,
    title: str | None = Form(default=None),
    caption: str | None = Form(default=None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    url, kind = save_upload_file(file, "reels", allow_video=True)
    if kind != "video":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File must be a video")

    thumbnail_url = generate_video_thumbnail(url)

    video = models.Reel(
        user_id=current_user.id, title=title, caption=caption, video_url=url,
        thumbnail_url=thumbnail_url,
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
