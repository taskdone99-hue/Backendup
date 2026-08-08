import os

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user

router = APIRouter(prefix="/api/share", tags=["share"])

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

CONTENT_MODELS = {
    models.ShareContentType.post: models.Post,
    models.ShareContentType.reel: models.Reel,
}


def _get_content_or_404(db: Session, content_type: models.ShareContentType, content_id: int):
    model = CONTENT_MODELS[content_type]
    obj = db.query(model).filter(model.id == content_id).first()
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{content_type.value.capitalize()} not found",
        )
    return obj


@router.post(
    "/internal", response_model=schemas.InternalShareResponse, status_code=status.HTTP_201_CREATED
)
def share_internal(
    payload: schemas.InternalShareRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Send a post or reel to one or more other users, as an in-app share (e.g. via DM)."""
    _get_content_or_404(db, payload.content_type, payload.content_id)

    recipients = (
        db.query(models.User).filter(models.User.id.in_(payload.recipient_ids)).all()
    )
    found_ids = {u.id for u in recipients}
    missing = [rid for rid in payload.recipient_ids if rid not in found_ids]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recipient(s) not found: {', '.join(str(m) for m in missing)}",
        )

    shares = []
    for recipient_id in payload.recipient_ids:
        share = models.Share(
            sender_id=current_user.id,
            recipient_id=recipient_id,
            content_type=payload.content_type,
            content_id=payload.content_id,
            message=payload.message,
        )
        db.add(share)
        shares.append(share)

    db.commit()
    for share in shares:
        db.refresh(share)

    return schemas.InternalShareResponse(message="Shared", shares=shares)


@router.get("/{post_id}/link", response_model=schemas.ShareLinkResponse)
def get_share_link(
    post_id: int,
    db: Session = Depends(get_db),
):
    """Generate a shareable deep-link URL for a post."""
    _get_content_or_404(db, models.ShareContentType.post, post_id)

    base = PUBLIC_BASE_URL or "https://app.example.com"
    return schemas.ShareLinkResponse(post_id=post_id, url=f"{base}/p/{post_id}")
