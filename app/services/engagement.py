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


def comments_count(db: Session, post_id: int) -> int:
    return (
        db.query(models.Comment)
        .filter(models.Comment.post_id == post_id)
        .count()
    )


def replies_count(db: Session, comment_id: int) -> int:
    return (
        db.query(models.Comment)
        .filter(models.Comment.parent_id == comment_id)
        .count()
    )
def shares_count(db: Session, post_id: int) -> int:
    return (
        db.query(models.Share)
        .filter(
            models.Share.content_type == models.ShareContentType.post,
            models.Share.content_id == post_id,
        )
        .count()
    )