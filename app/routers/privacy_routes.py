"""
Privacy: block, restrict, and mute a user; mute a conversation's
notifications. See app/services/privacy_service.py for the semantics of
each and where they're enforced elsewhere in the app (feeds, comments,
chat).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user

router = APIRouter(prefix="/api/privacy", tags=["privacy"])


def _get_target_user_or_404(db: Session, user_id: int) -> models.User:
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


# ---- block ----

@router.post("/block/{user_id}", response_model=schemas.BlockActionResponse)
def block_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if user_id == current_user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You can't block yourself")
    _get_target_user_or_404(db, user_id)

    existing = (
        db.query(models.UserBlock)
        .filter(models.UserBlock.blocker_id == current_user.id, models.UserBlock.blocked_id == user_id)
        .first()
    )
    if existing is None:
        db.add(models.UserBlock(blocker_id=current_user.id, blocked_id=user_id))

        # Blocking tears down any follow relationship in both directions —
        # a blocked account shouldn't still show up as a follower/following,
        # and their posts shouldn't appear in the follows-based Home feed.
        db.query(models.Follow).filter(
            models.Follow.follower_id.in_([current_user.id, user_id]),
            models.Follow.following_id.in_([current_user.id, user_id]),
        ).delete(synchronize_session=False)
        db.query(models.FollowRequest).filter(
            models.FollowRequest.requester_id.in_([current_user.id, user_id]),
            models.FollowRequest.target_id.in_([current_user.id, user_id]),
        ).delete(synchronize_session=False)
        db.commit()

    return schemas.BlockActionResponse(message="User blocked", is_blocked=True)


@router.delete("/block/{user_id}", response_model=schemas.BlockActionResponse)
def unblock_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = (
        db.query(models.UserBlock)
        .filter(models.UserBlock.blocker_id == current_user.id, models.UserBlock.blocked_id == user_id)
        .first()
    )
    if row is not None:
        db.delete(row)
        db.commit()
    return schemas.BlockActionResponse(message="User unblocked", is_blocked=False)


@router.get("/blocked", response_model=schemas.PaginatedUsersResponse)
def get_blocked_users(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    query = (
        db.query(models.User)
        .join(models.UserBlock, models.UserBlock.blocked_id == models.User.id)
        .filter(models.UserBlock.blocker_id == current_user.id)
    )
    total = query.count()
    users = query.order_by(models.UserBlock.created_at.desc()).offset(offset).limit(limit).all()
    return schemas.PaginatedUsersResponse(
        total=total, limit=limit, offset=offset,
        items=[schemas.UserSummaryOut.model_validate(u) for u in users],
    )


# ---- restrict ----

@router.post("/restrict/{user_id}", response_model=schemas.RestrictActionResponse)
def restrict_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if user_id == current_user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You can't restrict yourself")
    _get_target_user_or_404(db, user_id)

    existing = (
        db.query(models.UserRestrict)
        .filter(
            models.UserRestrict.restricter_id == current_user.id,
            models.UserRestrict.restricted_id == user_id,
        )
        .first()
    )
    if existing is None:
        db.add(models.UserRestrict(restricter_id=current_user.id, restricted_id=user_id))
        db.commit()
    return schemas.RestrictActionResponse(message="User restricted", is_restricted=True)


@router.delete("/restrict/{user_id}", response_model=schemas.RestrictActionResponse)
def unrestrict_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = (
        db.query(models.UserRestrict)
        .filter(
            models.UserRestrict.restricter_id == current_user.id,
            models.UserRestrict.restricted_id == user_id,
        )
        .first()
    )
    if row is not None:
        db.delete(row)
        db.commit()
    return schemas.RestrictActionResponse(message="User unrestricted", is_restricted=False)


@router.get("/restricted", response_model=schemas.PaginatedUsersResponse)
def get_restricted_users(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    query = (
        db.query(models.User)
        .join(models.UserRestrict, models.UserRestrict.restricted_id == models.User.id)
        .filter(models.UserRestrict.restricter_id == current_user.id)
    )
    total = query.count()
    users = query.order_by(models.UserRestrict.created_at.desc()).offset(offset).limit(limit).all()
    return schemas.PaginatedUsersResponse(
        total=total, limit=limit, offset=offset,
        items=[schemas.UserSummaryOut.model_validate(u) for u in users],
    )


# ---- mute (posts / stories) ----

@router.post("/mute/{user_id}", response_model=schemas.MuteActionResponse)
def mute_user(
    user_id: int,
    payload: schemas.MuteRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if user_id == current_user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You can't mute yourself")
    _get_target_user_or_404(db, user_id)

    row = (
        db.query(models.UserMute)
        .filter(models.UserMute.muter_id == current_user.id, models.UserMute.muted_id == user_id)
        .first()
    )
    if row is None:
        row = models.UserMute(
            muter_id=current_user.id, muted_id=user_id,
            mute_posts=payload.mute_posts, mute_stories=payload.mute_stories,
        )
        db.add(row)
    else:
        row.mute_posts = payload.mute_posts
        row.mute_stories = payload.mute_stories
    db.commit()
    return schemas.MuteActionResponse(
        message="Mute settings updated", mute_posts=row.mute_posts, mute_stories=row.mute_stories
    )


@router.delete("/mute/{user_id}", response_model=schemas.MuteActionResponse)
def unmute_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = (
        db.query(models.UserMute)
        .filter(models.UserMute.muter_id == current_user.id, models.UserMute.muted_id == user_id)
        .first()
    )
    if row is not None:
        db.delete(row)
        db.commit()
    return schemas.MuteActionResponse(message="User unmuted", mute_posts=False, mute_stories=False)


@router.get("/muted", response_model=schemas.PaginatedMutedUsersResponse)
def get_muted_users(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    query = db.query(models.UserMute).filter(models.UserMute.muter_id == current_user.id)
    total = query.count()
    rows = query.order_by(models.UserMute.created_at.desc()).offset(offset).limit(limit).all()
    items = [
        schemas.MuteOut(
            user=schemas.UserSummaryOut.model_validate(r.muted),
            mute_posts=r.mute_posts,
            mute_stories=r.mute_stories,
        )
        for r in rows
    ]
    return schemas.PaginatedMutedUsersResponse(total=total, limit=limit, offset=offset, items=items)
