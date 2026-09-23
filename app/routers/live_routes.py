"""
Instagram-style Live — metadata only. There's no streaming/RTMP/WebRTC
provider anywhere in this codebase, so this router doesn't add one: it
tracks who's live, who's watching (join/leave with timestamps), comments,
and likes, exactly the way the rest of this codebase stores social-graph
state around content rather than the media pipeline. A LiveSession is
ready to carry a `playback_url`/`ingest_url` pair the moment a real
streaming provider is wired in — that's a couple of additive columns, not
a redesign of anything here.

Kept as plain REST, not a WebSocket, even though this codebase already has
a WebSocket precedent (chat, notifications): the endpoint list this was
built against is a fixed set of REST routes with no live/streaming socket
in it, and REST keeps this cleanly testable and extensible on its own —
see the module docstring reasoning replicated from GET .../audio/trending
etc. for why literal-path routes are ordered ahead of `/{live_id}` below.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user, get_current_user_optional
from app.routers.content_routes import _visible_authors_clause
from app.routers.user_routes import _require_content_visible
from app.services.privacy_service import is_blocked

router = APIRouter(prefix="/api/live", tags=["live"])


# ---- internal helpers ----

def _get_live_or_404(db: Session, live_id: int) -> models.LiveSession:
    live = db.query(models.LiveSession).filter(models.LiveSession.id == live_id).first()
    if live is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Live session not found")
    return live


def _require_live_visible(db: Session, live: models.LiveSession, viewer_id: int | None) -> None:
    """Same rule as any other piece of content here: blocked (either
    direction) or a private host the viewer doesn't follow -> 404, so a
    live's existence doesn't leak to someone who shouldn't see it."""
    not_found = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Live session not found")
    if viewer_id is not None and is_blocked(db, viewer_id, live.user_id):
        raise not_found
    try:
        _require_content_visible(db, live.user, viewer_id)
    except HTTPException:
        raise not_found


def _viewer_count(db: Session, live_id: int) -> int:
    return (
        db.query(models.LiveViewer)
        .filter(models.LiveViewer.live_id == live_id, models.LiveViewer.left_at.is_(None))
        .count()
    )


def _likes_count(db: Session, live_id: int) -> int:
    return db.query(models.LiveLike).filter(models.LiveLike.live_id == live_id).count()


def _to_live_out(db: Session, live: models.LiveSession) -> schemas.LiveSessionOut:
    out = schemas.LiveSessionOut.model_validate(live)
    out.user = schemas.UserSummaryOut.model_validate(live.user)
    out.viewer_count = _viewer_count(db, live.id)
    out.likes_count = _likes_count(db, live.id)
    return out


# ---- start / details / join / leave / end ----

@router.post("", response_model=schemas.LiveSessionOut, status_code=status.HTTP_201_CREATED)
def start_live(
    payload: schemas.LiveCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    existing = (
        db.query(models.LiveSession)
        .filter(
            models.LiveSession.user_id == current_user.id,
            models.LiveSession.status == models.LiveStatus.live,
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have an active live session",
        )

    live = models.LiveSession(user_id=current_user.id, title=payload.title)
    db.add(live)
    db.commit()
    db.refresh(live)
    return _to_live_out(db, live)


@router.get("/active", response_model=schemas.PaginatedLiveSessionsResponse)
def get_active_lives(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    """Discovery — every currently-live session, newest-started first,
    same blocked/private-account visibility rule as the explore/reels
    feeds (see _visible_authors_clause)."""
    viewer_id = current_user.id if current_user else None

    query = (
        db.query(models.LiveSession)
        .join(models.User, models.LiveSession.user_id == models.User.id)
        .filter(models.LiveSession.status == models.LiveStatus.live)
        .filter(_visible_authors_clause(db, viewer_id))
    )
    total = query.count()
    lives = query.order_by(models.LiveSession.started_at.desc()).offset(offset).limit(limit).all()
    items = [_to_live_out(db, l) for l in lives]
    return schemas.PaginatedLiveSessionsResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/{live_id}", response_model=schemas.LiveSessionOut)
def get_live(
    live_id: int,
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    live = _get_live_or_404(db, live_id)
    _require_live_visible(db, live, current_user.id if current_user else None)
    return _to_live_out(db, live)


@router.post("/{live_id}/join", response_model=schemas.LiveActionResponse)
def join_live(
    live_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    live = _get_live_or_404(db, live_id)
    _require_live_visible(db, live, current_user.id)

    if live.status != models.LiveStatus.live:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This live has ended")
    if live.user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="You're hosting this live"
        )

    active_row = (
        db.query(models.LiveViewer)
        .filter(
            models.LiveViewer.live_id == live_id,
            models.LiveViewer.user_id == current_user.id,
            models.LiveViewer.left_at.is_(None),
        )
        .first()
    )
    if active_row is None:
        db.add(models.LiveViewer(live_id=live_id, user_id=current_user.id))
        db.commit()

    return schemas.LiveActionResponse(message="Joined live", live=_to_live_out(db, live))


@router.post("/{live_id}/leave", response_model=schemas.LiveActionResponse)
def leave_live(
    live_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    live = _get_live_or_404(db, live_id)

    active_row = (
        db.query(models.LiveViewer)
        .filter(
            models.LiveViewer.live_id == live_id,
            models.LiveViewer.user_id == current_user.id,
            models.LiveViewer.left_at.is_(None),
        )
        .first()
    )
    # Idempotent, same as unfollow_user above with no existing Follow row —
    # "leave" when you were never counted as watching just isn't an error.
    if active_row is not None:
        active_row.left_at = datetime.now(timezone.utc)
        db.commit()

    return schemas.LiveActionResponse(message="Left live", live=_to_live_out(db, live))


@router.post("/{live_id}/end", response_model=schemas.LiveActionResponse)
def end_live(
    live_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    live = _get_live_or_404(db, live_id)
    if live.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only the host can end this live"
        )
    if live.status == models.LiveStatus.ended:
        return schemas.LiveActionResponse(message="Live already ended", live=_to_live_out(db, live))

    live.status = models.LiveStatus.ended
    live.ended_at = datetime.now(timezone.utc)
    # Close out every still-active viewer row so the join/leave log is
    # complete and GET .../viewers/count reads 0 once a live has ended.
    db.query(models.LiveViewer).filter(
        models.LiveViewer.live_id == live_id, models.LiveViewer.left_at.is_(None)
    ).update({"left_at": live.ended_at})
    db.commit()
    db.refresh(live)
    return schemas.LiveActionResponse(message="Live ended", live=_to_live_out(db, live))


# ---- viewers ----

@router.get("/{live_id}/viewers", response_model=schemas.PaginatedLiveViewersResponse)
def get_live_viewers(
    live_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    """Currently-watching viewers (left_at IS NULL), most-recently-joined
    first."""
    live = _get_live_or_404(db, live_id)
    _require_live_visible(db, live, current_user.id if current_user else None)

    query = db.query(models.LiveViewer).filter(
        models.LiveViewer.live_id == live_id, models.LiveViewer.left_at.is_(None)
    )
    total = query.count()
    rows = query.order_by(models.LiveViewer.joined_at.desc()).offset(offset).limit(limit).all()
    items = [schemas.LiveViewerOut.model_validate(r) for r in rows]
    return schemas.PaginatedLiveViewersResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/{live_id}/viewers/count", response_model=schemas.LiveViewerCountResponse)
def get_live_viewer_count(
    live_id: int,
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    live = _get_live_or_404(db, live_id)
    _require_live_visible(db, live, current_user.id if current_user else None)
    return schemas.LiveViewerCountResponse(live_id=live_id, viewer_count=_viewer_count(db, live_id))


# ---- comments ----

@router.post(
    "/{live_id}/comments", response_model=schemas.LiveCommentOut, status_code=status.HTTP_201_CREATED
)
def add_live_comment(
    live_id: int,
    payload: schemas.LiveCommentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    live = _get_live_or_404(db, live_id)
    _require_live_visible(db, live, current_user.id)
    if live.status != models.LiveStatus.live:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This live has ended")

    comment = models.LiveComment(live_id=live_id, user_id=current_user.id, content=payload.content)
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return schemas.LiveCommentOut.model_validate(comment)


@router.get("/{live_id}/comments", response_model=schemas.PaginatedLiveCommentsResponse)
def get_live_comments(
    live_id: int,
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    live = _get_live_or_404(db, live_id)
    _require_live_visible(db, live, current_user.id if current_user else None)

    query = db.query(models.LiveComment).filter(models.LiveComment.live_id == live_id)
    total = query.count()
    rows = query.order_by(models.LiveComment.created_at.asc()).offset(offset).limit(limit).all()
    items = [schemas.LiveCommentOut.model_validate(r) for r in rows]
    return schemas.PaginatedLiveCommentsResponse(total=total, limit=limit, offset=offset, items=items)


@router.delete("/{live_id}/comments/{comment_id}", response_model=schemas.MessageResponse)
def delete_live_comment(
    live_id: int,
    comment_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    live = _get_live_or_404(db, live_id)
    comment = (
        db.query(models.LiveComment)
        .filter(models.LiveComment.id == comment_id, models.LiveComment.live_id == live_id)
        .first()
    )
    if comment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")

    # No per-live "moderator" role exists in this codebase (unlike
    # Instagram's assignable live moderators) — scoped to who's
    # unambiguously allowed to remove it: the host, the commenter
    # themselves, or a global admin (see models.User.is_admin).
    is_host = current_user.id == live.user_id
    is_author = current_user.id == comment.user_id
    if not (is_host or is_author or current_user.is_admin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the host, the comment's author, or an admin can remove this comment",
        )

    db.delete(comment)
    db.commit()
    return schemas.MessageResponse(message="Comment removed")


# ---- likes ----

@router.post("/{live_id}/like", response_model=schemas.LiveLikeActionResponse)
def like_live(
    live_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    live = _get_live_or_404(db, live_id)
    _require_live_visible(db, live, current_user.id)

    existing = (
        db.query(models.LiveLike)
        .filter(models.LiveLike.live_id == live_id, models.LiveLike.user_id == current_user.id)
        .first()
    )
    if existing is None:
        db.add(models.LiveLike(live_id=live_id, user_id=current_user.id))
        db.commit()

    return schemas.LiveLikeActionResponse(
        message="Liked" if existing is None else "Already liked",
        liked=True,
        likes_count=_likes_count(db, live_id),
    )


@router.delete("/{live_id}/like", response_model=schemas.LiveLikeActionResponse)
def unlike_live(
    live_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    live = _get_live_or_404(db, live_id)

    existing = (
        db.query(models.LiveLike)
        .filter(models.LiveLike.live_id == live_id, models.LiveLike.user_id == current_user.id)
        .first()
    )
    if existing is not None:
        db.delete(existing)
        db.commit()

    return schemas.LiveLikeActionResponse(
        message="Like removed", liked=False, likes_count=_likes_count(db, live_id)
    )
