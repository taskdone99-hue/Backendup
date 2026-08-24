from sqlalchemy.orm import Session

from app import models


def likes_count(
    db: Session,
    target_type: models.LikeTargetType,
    target_id: int
) -> int:
    return (
        db.query(models.Like)
        .filter(
            models.Like.target_type == target_type,
            models.Like.target_id == target_id
        )
        .count()
    )


def is_liked_by(
    db: Session,
    user_id: int | None,
    target_type: models.LikeTargetType,
    target_id: int,
) -> bool:
    if user_id is None:
        return False

    return (
        db.query(models.Like)
        .filter(
            models.Like.target_type == target_type,
            models.Like.target_id == target_id,
            models.Like.user_id == user_id,
        )
        .first()
        is not None
    )


def get_like_id(
    db: Session,
    user_id: int | None,
    target_type: models.LikeTargetType,
    target_id: int,
) -> int | None:
    """Returns the viewer's own Like.id for this target, or None if they
    haven't liked it — lets a response include the id a client would need
    to call DELETE /api/likes/{like_id} without a separate lookup."""
    if user_id is None:
        return None

    like = (
        db.query(models.Like.id)
        .filter(
            models.Like.target_type == target_type,
            models.Like.target_id == target_id,
            models.Like.user_id == user_id,
        )
        .first()
    )
    return like[0] if like else None


def comments_count(db: Session, post_id: int | None = None, reel_id: int | None = None) -> int:
    query = db.query(models.Comment)
    if reel_id is not None:
        return query.filter(models.Comment.reel_id == reel_id).count()
    return query.filter(models.Comment.post_id == post_id).count()


def shares_count(db: Session, content_type: models.ShareContentType, content_id: int) -> int:
    return (
        db.query(models.Share)
        .filter(
            models.Share.content_type == content_type,
            models.Share.content_id == content_id,
        )
        .count()
    )


def is_saved_by(
    db: Session,
    user_id: int | None,
    target_id: int,
    target_type: "models.SavedItemType" = None,
) -> bool:
    if user_id is None:
        return False
    if target_type is None:
        target_type = models.SavedItemType.post

    return (
        db.query(models.SavedItem)
        .filter(
            models.SavedItem.user_id == user_id,
            models.SavedItem.target_type == target_type,
            models.SavedItem.target_id == target_id,
        )
        .first()
        is not None
    )


def replies_count(db: Session, comment_id: int) -> int:
    return (
        db.query(models.Comment)
        .filter(models.Comment.parent_id == comment_id)
        .count()
    )