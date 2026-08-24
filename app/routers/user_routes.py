from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user, get_current_user_optional
from app.services.media_service import delete_media_file, save_upload_file
from app.routers.content_routes import _to_post_detail

router = APIRouter(tags=["users"])


# ---- internal helpers ----

def _get_user_or_404(db: Session, user_id: int) -> models.User:
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


def _is_following(db: Session, follower_id: int, following_id: int) -> bool:
    return (
        db.query(models.Follow)
        .filter(
            models.Follow.follower_id == follower_id,
            models.Follow.following_id == following_id,
        )
        .first()
        is not None
    )


def _to_summary(db: Session, user: models.User, viewer_id: int | None) -> schemas.UserSummaryOut:
    summary = schemas.UserSummaryOut.model_validate(user)
    if viewer_id is not None and viewer_id != user.id:
        summary.is_following = _is_following(db, viewer_id, user.id)
    return summary


# ==========================================================================
# 2. User Profile
# ==========================================================================

@router.get("/api/users/{user_id}", response_model=schemas.UserProfileOut)
def get_user_profile(user_id: int, db: Session = Depends(get_db)):
    user = _get_user_or_404(db, user_id)
    return user


@router.put("/api/users/{user_id}", response_model=schemas.UserProfileOut)
def update_user_profile(
    user_id: int,
    payload: schemas.UserProfileUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if current_user.id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only update your own profile",
        )

    updates = payload.model_dump(exclude_unset=True)

    if "username" in updates and updates["username"] != current_user.username:
        taken = (
            db.query(models.User)
            .filter(models.User.username == updates["username"], models.User.id != current_user.id)
            .first()
            is not None
        )
        if taken:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Username is already taken"
            )

    for field, value in updates.items():
        setattr(current_user, field, value)

    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/api/users/{user_id}/avatar", response_model=schemas.AvatarUploadResponse)
def upload_avatar(
    user_id: int,
    file: UploadFile,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if current_user.id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only update your own avatar",
        )

    old_url = current_user.avatar_url
    url, _kind = save_upload_file(file, "avatars", allow_video=False)

    current_user.avatar_url = url
    db.commit()
    if old_url:
        delete_media_file(old_url)

    return schemas.AvatarUploadResponse(message="Avatar updated", avatar_url=url)


@router.delete("/api/users/{user_id}/avatar", response_model=schemas.MessageResponse)
def delete_avatar(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Removes the avatar, reverting to no profile photo (Instagram's 'Remove Current Photo')."""
    if current_user.id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only update your own avatar",
        )

    old_url = current_user.avatar_url
    current_user.avatar_url = None
    db.commit()
    if old_url:
        delete_media_file(old_url)

    return schemas.MessageResponse(message="Avatar removed")


@router.get("/api/users/{user_id}/posts", response_model=schemas.PaginatedPostDetailResponse)
def get_user_posts(
    user_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    _get_user_or_404(db, user_id)

    query = db.query(models.Post).filter(models.Post.user_id == user_id)
    total = query.count()
    posts = (
        query.order_by(models.Post.created_at.desc()).offset(offset).limit(limit).all()
    )
    viewer_id = current_user.id if current_user else None
    items = [_to_post_detail(db, p, viewer_id) for p in posts]
    return schemas.PaginatedPostDetailResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/api/users/{user_id}/reels", response_model=schemas.PaginatedReelsResponse)
def get_user_reels(
    user_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    _get_user_or_404(db, user_id)

    query = db.query(models.Reel).filter(models.Reel.user_id == user_id)
    total = query.count()
    items = (
        query.order_by(models.Reel.created_at.desc()).offset(offset).limit(limit).all()
    )
    return schemas.PaginatedReelsResponse(total=total, limit=limit, offset=offset, items=items)


@router.get(
    "/api/users/{user_id}/saved/reels", response_model=schemas.PaginatedReelDetailResponse
)
def get_saved_reels(
    user_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Flat list of the logged-in user's saved reels — same response shape as
    GET /api/users/:id/reels (their own posted reels), so a profile screen
    can render both with the same reel-grid component. For the mixed
    all/posts/reels/audio/series view, use GET /api/users/:id/saved instead.
    """
    if current_user.id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only view your own saved reels",
        )

    from app.routers.content_routes import _to_reel_detail

    query = (
        db.query(models.Reel, models.SavedItem.created_at)
        .join(
            models.SavedItem,
            (models.SavedItem.target_type == models.SavedItemType.reel)
            & (models.SavedItem.target_id == models.Reel.id),
        )
        .filter(models.SavedItem.user_id == user_id)
    )
    total = query.count()
    rows = query.order_by(models.SavedItem.created_at.desc()).offset(offset).limit(limit).all()

    items = [_to_reel_detail(db, reel, current_user.id) for reel, _saved_at in rows]
    return schemas.PaginatedReelDetailResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/api/users/{user_id}/saved", response_model=schemas.PaginatedSavedResponse)
def get_saved_posts(
    user_id: int,
    category: str = Query(
        "all",
        pattern="^(all|posts|reels|audio|series)$",
        description="all | posts | reels | audio | series",
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    # Saved posts are private — Instagram only ever shows them to their owner.
    if current_user.id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only view your own saved posts",
        )

    # Same underlying data/logic as GET /api/saved — kept here too since
    # this is the pre-existing spec'd path.
    from app.routers.saved_routes import get_saved_items

    return get_saved_items(
        category=category, limit=limit, offset=offset, db=db, current_user=current_user
    )


@router.get("/api/users/{user_id}/stats", response_model=schemas.UserStatsOut)
def get_user_stats(user_id: int, db: Session = Depends(get_db)):
    _get_user_or_404(db, user_id)

    normal_posts_count = db.query(models.Post).filter(models.Post.user_id == user_id).count()
    reels_count = db.query(models.Reel).filter(models.Reel.user_id == user_id).count()
    # posts_count is total content — normal posts + reels — matching how a
    # profile's post count is usually shown on Instagram (reels count
    # toward the grid total too). reels_count stays as just reels, for
    # whatever surface shows that separately (e.g. a Reels tab count).
    posts_count = normal_posts_count + reels_count
    followers_count = (
        db.query(models.Follow).filter(models.Follow.following_id == user_id).count()
    )
    following_count = (
        db.query(models.Follow).filter(models.Follow.follower_id == user_id).count()
    )

    return schemas.UserStatsOut(
        user_id=user_id,
        posts_count=posts_count,
        reels_count=reels_count,
        followers_count=followers_count,
        following_count=following_count,
    )


# ==========================================================================
# 3. Follow System
# ==========================================================================

@router.post("/api/follow/{user_id}", response_model=schemas.FollowStatusResponse)
def follow_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="You can't follow yourself"
        )
    target = _get_user_or_404(db, user_id)

    existing = (
        db.query(models.Follow)
        .filter(
            models.Follow.follower_id == current_user.id,
            models.Follow.following_id == target.id,
        )
        .first()
    )
    if existing is None:
        db.add(models.Follow(follower_id=current_user.id, following_id=target.id))
        db.commit()

    return schemas.FollowStatusResponse(message=f"Now following {target.username}", following=True)


@router.delete("/api/follow/{user_id}", response_model=schemas.FollowStatusResponse)
def unfollow_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    target = _get_user_or_404(db, user_id)

    existing = (
        db.query(models.Follow)
        .filter(
            models.Follow.follower_id == current_user.id,
            models.Follow.following_id == target.id,
        )
        .first()
    )
    if existing is not None:
        db.delete(existing)
        db.commit()

    return schemas.FollowStatusResponse(
        message=f"Unfollowed {target.username}", following=False
    )


@router.get("/api/users/{user_id}/followers", response_model=schemas.PaginatedUsersResponse)
def get_followers(
    user_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    viewer: models.User | None = Depends(get_current_user_optional),
):
    """Users who follow :id."""
    _get_user_or_404(db, user_id)

    query = (
        db.query(models.User)
        .join(models.Follow, models.Follow.follower_id == models.User.id)
        .filter(models.Follow.following_id == user_id)
    )
    total = query.count()
    users = (
        query.order_by(models.Follow.created_at.desc()).offset(offset).limit(limit).all()
    )
    items = [_to_summary(db, u, viewer.id if viewer else None) for u in users]
    return schemas.PaginatedUsersResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/api/users/{user_id}/following", response_model=schemas.PaginatedUsersResponse)
def get_following(
    user_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    viewer: models.User | None = Depends(get_current_user_optional),
):
    """Users that :id follows."""
    _get_user_or_404(db, user_id)

    query = (
        db.query(models.User)
        .join(models.Follow, models.Follow.following_id == models.User.id)
        .filter(models.Follow.follower_id == user_id)
    )
    total = query.count()
    users = (
        query.order_by(models.Follow.created_at.desc()).offset(offset).limit(limit).all()
    )
    items = [_to_summary(db, u, viewer.id if viewer else None) for u in users]
    return schemas.PaginatedUsersResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/api/users/{user_id}/suggested", response_model=schemas.PaginatedUsersResponse)
def get_suggested_users(
    user_id: int,
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Simple suggestion strategy: "people followed by people you follow"
    (2nd-degree connections), excluding yourself and anyone you already
    follow. Falls back to newest active users if that set is empty (e.g. for
    brand-new accounts with nothing to base suggestions on).
    """
    if current_user.id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only view your own suggestions",
        )

    following_ids_subq = (
        db.query(models.Follow.following_id)
        .filter(models.Follow.follower_id == current_user.id)
    )
    following_ids = [row[0] for row in following_ids_subq.all()]
    exclude_ids = set(following_ids) | {current_user.id}

    suggestions: list[models.User] = []
    if following_ids:
        second_degree = (
            db.query(models.User)
            .join(models.Follow, models.Follow.following_id == models.User.id)
            .filter(
                models.Follow.follower_id.in_(following_ids),
                ~models.User.id.in_(exclude_ids),
            )
            .distinct()
            .limit(limit + offset)
            .all()
        )
        suggestions = second_degree

    if len(suggestions) <= offset:
        # Not enough 2nd-degree connections — fill with newest active users.
        already = exclude_ids | {u.id for u in suggestions}
        fallback = (
            db.query(models.User)
            .filter(models.User.is_active == True, ~models.User.id.in_(already))
            .order_by(models.User.created_at.desc())
            .limit(limit + offset)
            .all()
        )
        suggestions.extend(fallback)

    page = suggestions[offset: offset + limit]
    items = [_to_summary(db, u, current_user.id) for u in page]
    return schemas.PaginatedUsersResponse(
        total=len(suggestions), limit=limit, offset=offset, items=items
    )
