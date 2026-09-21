"""
Audio ("sounds") — details, which reels use a given sound, and a
trending/discovery list. See models.Audio and Reel.audio_id.

Reels get attached to an Audio row two ways: explicitly on creation
(POST /api/reels with audio_id) or implicitly when remixed
(POST /api/reels/{id}/audio-remix backfills one if the original didn't
have one yet — see content_routes.remix_reel_audio).
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import get_current_user_optional
from app.database import get_db
from app import models, schemas
from app.routers.content_routes import _to_reel_detail, _visible_authors_clause

router = APIRouter(prefix="/api/audio", tags=["audio"])


def _reels_count(db: Session, audio_id: int) -> int:
    return db.query(models.Reel).filter(models.Reel.audio_id == audio_id).count()


def _get_audio_or_404(db: Session, audio_id: int) -> models.Audio:
    audio = db.query(models.Audio).filter(models.Audio.id == audio_id).first()
    if audio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio not found")
    return audio


@router.get("/trending", response_model=schemas.PaginatedAudioResponse)
def get_trending_audio(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    days: int = Query(7, ge=1, le=90, description="Trending window, in days"),
    db: Session = Depends(get_db),
):
    """
    Discovery list of sounds, ranked by how many reels created in the
    trailing `days` window used them — the "audio remixes" / trending-sounds
    surface a client would show when picking a sound to remix.

    NOTE: registered before /{audio_id} for the same route-ordering reason
    documented on the reels feed/trending routes in content_routes.py.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    counts_subq = (
        db.query(
            models.Reel.audio_id.label("audio_id"),
            func.count(models.Reel.id).label("usage_count"),
        )
        .filter(models.Reel.audio_id.isnot(None), models.Reel.created_at >= since)
        .group_by(models.Reel.audio_id)
        .subquery()
    )

    query = (
        db.query(models.Audio, counts_subq.c.usage_count)
        .join(counts_subq, models.Audio.id == counts_subq.c.audio_id)
    )
    total = query.count()
    rows = (
        query.order_by(counts_subq.c.usage_count.desc(), models.Audio.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    items = []
    for audio, usage_count in rows:
        out = schemas.AudioDetailOut.model_validate(audio)
        out.reels_count = usage_count
        items.append(out)

    return schemas.PaginatedAudioResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/{audio_id}", response_model=schemas.AudioDetailOut)
def get_audio_details(audio_id: int, db: Session = Depends(get_db)):
    audio = _get_audio_or_404(db, audio_id)
    out = schemas.AudioDetailOut.model_validate(audio)
    out.reels_count = _reels_count(db, audio_id)
    return out


@router.get("/{audio_id}/reels", response_model=schemas.PaginatedReelDetailResponse)
def get_reels_using_audio(
    audio_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    """All reels that use this sound, newest first — respects the same
    blocked/private-account visibility rules as the main reels feed."""
    _get_audio_or_404(db, audio_id)
    viewer_id = current_user.id if current_user else None

    query = (
        db.query(models.Reel)
        .join(models.User, models.Reel.user_id == models.User.id)
        .filter(models.Reel.audio_id == audio_id)
        .filter(_visible_authors_clause(db, viewer_id))
    )
    total = query.count()
    reels = query.order_by(models.Reel.created_at.desc()).offset(offset).limit(limit).all()
    items = [_to_reel_detail(db, r, viewer_id) for r in reels]
    return schemas.PaginatedReelDetailResponse(total=total, limit=limit, offset=offset, items=items)
