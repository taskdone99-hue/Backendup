"""
Comments (on posts) and the generic like system (posts, reels, and comments
can all be liked through the same /api/likes endpoints — see models.Like).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app import models, schemas
from app.auth import get_current_user, get_current_user_optional
from app.database import get_db
from app.services import engagement

router = APIRouter(tags=["comments"])


# ---- internal helpers ----

def _get_post_or_404(db: Session, post_id: int) -> models.Post:
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if post is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    return post


def _get_reel_or_404(db: Session, reel_id: int) -> models.Reel:
    reel = db.query(models.Reel).filter(models.Reel.id == reel_id).first()
    if reel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reel not found")
    return reel


def _get_comment_or_404(db: Session, comment_id: int) -> models.Comment:
    comment = db.query(models.Comment).filter(models.Comment.id == comment_id).first()
    if comment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")
    return comment


def _to_comment_out(
    db: Session, comment: models.Comment, viewer_id: int | None
) -> schemas.CommentOut:
    out = schemas.CommentOut.model_validate(comment)
    out.user = schemas.UserSummaryOut.model_validate(comment.user)
    out.likes_count = engagement.likes_count(db, models.LikeTargetType.comment, comment.id)
    out.replies_count = engagement.replies_count(db, comment.id)
    out.like_id = engagement.get_like_id(db, viewer_id, models.LikeTargetType.comment, comment.id)
    out.is_liked = out.like_id is not None
    return out


TARGET_MODELS = {
    models.LikeTargetType.post: models.Post,
    models.LikeTargetType.reel: models.Reel,
    models.LikeTargetType.comment: models.Comment,
}


def _target_exists(db: Session, target_type: models.LikeTargetType, target_id: int) -> bool:
    model = TARGET_MODELS[target_type]
    return db.query(model).filter(model.id == target_id).first() is not None


# ==========================================================================
# Comments
# ==========================================================================

@router.post(
    "/api/posts/{post_id}/comments",
    response_model=schemas.CommentOut,
    status_code=status.HTTP_201_CREATED,
)
def add_comment(
    post_id: int,
    payload: schemas.CommentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _get_post_or_404(db, post_id)
    comment = models.Comment(post_id=post_id, user_id=current_user.id, content=payload.content)
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return _to_comment_out(db, comment, current_user.id)


@router.get("/api/posts/{post_id}/comments", response_model=schemas.PaginatedCommentsResponse)
def get_comments(
    post_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    """
    Top-level comments only (paginated). Each item's `replies_count` tells
    the client whether there's a thread worth fetching via
    POST /api/comments/:id/reply's replies — one level deep, no nested pagination.
    """
    _get_post_or_404(db, post_id)
    query = db.query(models.Comment).options(joinedload(models.Comment.user)).filter(
        models.Comment.post_id == post_id, models.Comment.parent_id.is_(None)
    )
    total = query.count()
    comments = query.order_by(models.Comment.created_at.asc()).offset(offset).limit(limit).all()
    viewer_id = current_user.id if current_user else None
    items = [_to_comment_out(db, c, viewer_id) for c in comments]
    return schemas.PaginatedCommentsResponse(total=total, limit=limit, offset=offset, items=items)


@router.post(
    "/api/reels/{reel_id}/comments",
    response_model=schemas.CommentOut,
    status_code=status.HTTP_201_CREATED,
)
def add_reel_comment(
    reel_id: int,
    payload: schemas.CommentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _get_reel_or_404(db, reel_id)
    comment = models.Comment(reel_id=reel_id, user_id=current_user.id, content=payload.content)
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return _to_comment_out(db, comment, current_user.id)


@router.get("/api/reels/{reel_id}/comments", response_model=schemas.PaginatedCommentsResponse)
def get_reel_comments(
    reel_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    _get_reel_or_404(db, reel_id)
    query = db.query(models.Comment).options(joinedload(models.Comment.user)).filter(
        models.Comment.reel_id == reel_id,
        models.Comment.parent_id.is_(None),
    )
    total = query.count()
    comments = query.order_by(models.Comment.created_at.asc()).offset(offset).limit(limit).all()
    viewer_id = current_user.id if current_user else None
    items = [_to_comment_out(db, c, viewer_id) for c in comments]
    return schemas.PaginatedCommentsResponse(total=total, limit=limit, offset=offset, items=items)


@router.post(
    "/api/comments/{comment_id}/reply",
    response_model=schemas.CommentOut,
    status_code=status.HTTP_201_CREATED,
)
def reply_to_comment(
    comment_id: int,
    payload: schemas.CommentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    parent = _get_comment_or_404(db, comment_id)
    if parent.parent_id is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Replies can only be made to top-level comments")
    reply = models.Comment(
        post_id=parent.post_id,
        reel_id=parent.reel_id,
        user_id=current_user.id,
        parent_id=parent.id,
        content=payload.content,
    )
    db.add(reply)
    db.commit()
    db.refresh(reply)
    return _to_comment_out(db, reply, current_user.id)


@router.get("/api/comments/{comment_id}/replies", response_model=schemas.PaginatedCommentsResponse)
def get_comment_replies(
    comment_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    _get_comment_or_404(db, comment_id)
    query = db.query(models.Comment).options(joinedload(models.Comment.user)).filter(
        models.Comment.parent_id == comment_id
    )
    total = query.count()
    replies = query.order_by(models.Comment.created_at.asc()).offset(offset).limit(limit).all()
    viewer_id = current_user.id if current_user else None
    items = [_to_comment_out(db, c, viewer_id) for c in replies]
    return schemas.PaginatedCommentsResponse(total=total, limit=limit, offset=offset, items=items)


@router.post("/api/comments/{comment_id}/like", response_model=schemas.LikeActionResponse)
def like_comment(
    comment_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Idempotent — liking an already-liked comment just returns the existing like."""
    _get_comment_or_404(db, comment_id)
    existing = (
        db.query(models.Like)
        .filter(
            models.Like.user_id == current_user.id,
            models.Like.target_type == models.LikeTargetType.comment,
            models.Like.target_id == comment_id,
        )
        .first()
    )
    if existing is None:
        existing = models.Like(
            user_id=current_user.id,
            target_type=models.LikeTargetType.comment,
            target_id=comment_id,
        )
        db.add(existing)
        db.commit()
        db.refresh(existing)

    count = engagement.likes_count(db, models.LikeTargetType.comment, comment_id)
    like_out = schemas.LikeOut.model_validate(existing)
    like_out.user = schemas.UserSummaryOut.model_validate(existing.user)
    return schemas.LikeActionResponse(message="Comment liked", like=like_out, likes_count=count)


@router.delete("/api/comments/{comment_id}", response_model=schemas.MessageResponse)
def delete_comment(
    comment_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    comment = _get_comment_or_404(db, comment_id)
    if comment.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only delete your own comments"
        )

    # Replies are one level deep in this API (see get_comments), so a
    # comment's children are exactly the rows with parent_id == comment.id.
    reply_ids = [
        row[0]
        for row in db.query(models.Comment.id)
        .filter(models.Comment.parent_id == comment_id)
        .all()
    ]
    all_ids = [comment_id] + reply_ids

    db.query(models.Like).filter(
        models.Like.target_type == models.LikeTargetType.comment,
        models.Like.target_id.in_(all_ids),
    ).delete(synchronize_session=False)

    # Replies first, parent second — two statements, not one combined
    # `id IN (...)` delete. parent_id is a self-referencing FK with no
    # ON DELETE CASCADE, so on MySQL/InnoDB a single multi-row DELETE that
    # includes both a comment and its own reply can fail with a foreign key
    # constraint violation depending on row-deletion order within that
    # statement. Deleting children before the parent, as separate
    # statements, sidesteps that ordering issue entirely.
    if reply_ids:
        db.query(models.Comment).filter(models.Comment.id.in_(reply_ids)).delete(
            synchronize_session=False
        )
    db.query(models.Comment).filter(models.Comment.id == comment_id).delete(
        synchronize_session=False
    )
    db.commit()
    return schemas.MessageResponse(message="Comment deleted")


# ==========================================================================
# Likes (generic — post / reel / comment)
# ==========================================================================

@router.post(
    "/api/likes", response_model=schemas.LikeActionResponse, status_code=status.HTTP_201_CREATED
)
def like_target(
    payload: schemas.LikeCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Idempotent — liking something twice just returns the existing like, no duplicate row."""
    if not _target_exists(db, payload.target_type, payload.target_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{payload.target_type.value.capitalize()} not found",
        )

    existing = (
        db.query(models.Like)
        .filter(
            models.Like.user_id == current_user.id,
            models.Like.target_type == payload.target_type,
            models.Like.target_id == payload.target_id,
        )
        .first()
    )
    if existing is None:
        existing = models.Like(
            user_id=current_user.id,
            target_type=payload.target_type,
            target_id=payload.target_id,
        )
        db.add(existing)
        try:
            db.commit()
        except IntegrityError:
            # Two near-simultaneous taps (e.g. a rapid double-tap-to-like)
            # both passed the check above and both tried to insert — the
            # unique constraint on (user_id, target_type, target_id) caught
            # the second one. Not an error from the client's point of view;
            # fall through to the row the other request just created.
            db.rollback()
            existing = (
                db.query(models.Like)
                .filter(
                    models.Like.user_id == current_user.id,
                    models.Like.target_type == payload.target_type,
                    models.Like.target_id == payload.target_id,
                )
                .first()
            )
        else:
            db.refresh(existing)

    count = engagement.likes_count(db, payload.target_type, payload.target_id)
    like_out = schemas.LikeOut.model_validate(existing)
    like_out.user = schemas.UserSummaryOut.model_validate(existing.user)
    return schemas.LikeActionResponse(message="Liked", like=like_out, likes_count=count)


@router.delete("/api/likes/{like_id}", response_model=schemas.MessageResponse)
def unlike(
    like_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    like = db.query(models.Like).filter(models.Like.id == like_id).first()
    if like is None:
        # Already gone — unliking twice shouldn't be an error for the client.
        return schemas.MessageResponse(message="Already unliked")
    if like.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only remove your own like"
        )

    db.delete(like)
    db.commit()
    return schemas.MessageResponse(message="Unliked")


@router.delete("/api/likes", response_model=schemas.MessageResponse)
def unlike_target(
    target_type: models.LikeTargetType = Query(...),
    target_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Unlike by (target_type, target_id) instead of like_id — a post/reel/
    comment's `is_liked: true` doesn't come with its like_id attached, so a
    client showing a filled-in heart on a feed item had no id to call
    DELETE /api/likes/{like_id} with. This is the endpoint for that case;
    /api/likes/{like_id} is kept for callers that do have the id.
    """
    like = (
        db.query(models.Like)
        .filter(
            models.Like.user_id == current_user.id,
            models.Like.target_type == target_type,
            models.Like.target_id == target_id,
        )
        .first()
    )
    if like is not None:
        db.delete(like)
        db.commit()

    return schemas.MessageResponse(message="Unliked")


@router.get("/api/posts/{post_id}/likes", response_model=schemas.PaginatedLikesResponse)
def get_post_likes(
    post_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    _get_post_or_404(db, post_id)
    query = (
        db.query(models.User)
        .join(models.Like, models.Like.user_id == models.User.id)
        .filter(
            models.Like.target_type == models.LikeTargetType.post,
            models.Like.target_id == post_id,
        )
        .order_by(models.Like.created_at.desc())
    )
    total = query.count()
    users = query.offset(offset).limit(limit).all()
    viewer_id = current_user.id if current_user else None

    items = []
    for user in users:
        summary = schemas.UserSummaryOut.model_validate(user)
        if viewer_id is not None and viewer_id != user.id:
            summary.is_following = (
                db.query(models.Follow)
                .filter(
                    models.Follow.follower_id == viewer_id,
                    models.Follow.following_id == user.id,
                )
                .first()
                is not None
            )
        items.append(summary)

    return schemas.PaginatedLikesResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/api/reels/{reel_id}/likes", response_model=schemas.PaginatedLikesResponse)
def get_reel_likes(
    reel_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    _get_reel_or_404(db, reel_id)
    query = (
        db.query(models.User)
        .join(models.Like, models.Like.user_id == models.User.id)
        .filter(
            models.Like.target_type == models.LikeTargetType.reel,
            models.Like.target_id == reel_id,
        )
        .order_by(models.Like.created_at.desc())
    )
    total = query.count()
    users = query.offset(offset).limit(limit).all()
    viewer_id = current_user.id if current_user else None
    items = []
    for user in users:
        summary = schemas.UserSummaryOut.model_validate(user)
        if viewer_id is not None and viewer_id != user.id:
            summary.is_following = db.query(models.Follow).filter(
                models.Follow.follower_id == viewer_id,
                models.Follow.following_id == user.id,
            ).first() is not None
        items.append(summary)
    return schemas.PaginatedLikesResponse(total=total, limit=limit, offset=offset, items=items)
