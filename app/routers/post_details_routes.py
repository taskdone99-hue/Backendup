"""
Post Details: Tag People, Add Members, Add Music, Add Location.

All four operate on an existing post (created via POST /api/posts in
content_routes.py) and are owner-only to mutate, same as post captions.
Tag People and Add Members are separate join tables (post_tags,
post_members) since a post can have many of each; Add Music and Add
Location are single nullable-column groups on Post itself, since a post
has at most one of each — see the comment on those columns in models.py.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user

router = APIRouter(prefix="/api/posts", tags=["post-details"])


# ---- internal helpers ----

def _get_post_or_404(db: Session, post_id: int) -> models.Post:
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if post is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    return post


def _get_owned_post_or_404(db: Session, post_id: int, current_user: models.User) -> models.Post:
    post = _get_post_or_404(db, post_id)
    if post.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only edit your own posts"
        )
    return post


def _all_tags(db: Session, post_id: int) -> list[models.PostTag]:
    return (
        db.query(models.PostTag)
        .filter(models.PostTag.post_id == post_id)
        .order_by(models.PostTag.tagged_at)
        .all()
    )


def _all_members(db: Session, post_id: int) -> list[models.PostMember]:
    return (
        db.query(models.PostMember)
        .filter(models.PostMember.post_id == post_id)
        .order_by(models.PostMember.added_at)
        .all()
    )


# ==========================================================================
# Tag People
# ==========================================================================

@router.post(
    "/{post_id}/tags", response_model=schemas.PostTagsResponse, status_code=status.HTTP_201_CREATED
)
def tag_people(
    post_id: int,
    payload: schemas.TagPeopleRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = _get_owned_post_or_404(db, post_id, current_user)

    user_ids = [t.user_id for t in payload.tags]
    found_ids = {
        u.id for u in db.query(models.User.id).filter(models.User.id.in_(user_ids)).all()
    }
    missing = [uid for uid in user_ids if uid not in found_ids]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User(s) not found: {', '.join(str(m) for m in missing)}",
        )

    already_tagged = {
        t.user_id
        for t in db.query(models.PostTag.user_id)
        .filter(models.PostTag.post_id == post_id, models.PostTag.user_id.in_(user_ids))
        .all()
    }

    for tag in payload.tags:
        if tag.user_id in already_tagged:
            continue
        db.add(models.PostTag(
            post_id=post.id,
            user_id=tag.user_id,
            x_position=tag.x_position,
            y_position=tag.y_position,
        ))
    db.commit()

    return schemas.PostTagsResponse(message="Tagged", tags=_all_tags(db, post_id))


@router.get("/{post_id}/tags", response_model=schemas.PostTagsResponse)
def get_tags(post_id: int, db: Session = Depends(get_db)):
    _get_post_or_404(db, post_id)
    return schemas.PostTagsResponse(message="", tags=_all_tags(db, post_id))


@router.delete("/{post_id}/tags/{user_id}", response_model=schemas.MessageResponse)
def remove_tag(
    post_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = _get_post_or_404(db, post_id)
    tag = (
        db.query(models.PostTag)
        .filter(models.PostTag.post_id == post_id, models.PostTag.user_id == user_id)
        .first()
    )
    if tag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")

    # The post owner can remove any tag; a tagged person can also remove
    # themselves (same as Instagram letting you untag yourself).
    if current_user.id != post.user_id and current_user.id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the post owner or the tagged user can remove this tag",
        )

    db.delete(tag)
    db.commit()
    return schemas.MessageResponse(message="Tag removed")


# ==========================================================================
# Add Members
# ==========================================================================

@router.post(
    "/{post_id}/members", response_model=schemas.PostMembersResponse, status_code=status.HTTP_201_CREATED
)
def add_member(
    post_id: int,
    payload: schemas.PostMemberAddRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = _get_owned_post_or_404(db, post_id, current_user)

    member_user = db.query(models.User).filter(models.User.id == payload.user_id).first()
    if member_user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if member_user.id == post.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The post owner doesn't need to be added as a member",
        )

    existing = (
        db.query(models.PostMember)
        .filter(models.PostMember.post_id == post_id, models.PostMember.user_id == payload.user_id)
        .first()
    )
    if existing is None:
        db.add(models.PostMember(post_id=post_id, user_id=payload.user_id))
        db.commit()

    return schemas.PostMembersResponse(message="Member added", members=_all_members(db, post_id))


@router.get("/{post_id}/members", response_model=schemas.PostMembersResponse)
def get_members(post_id: int, db: Session = Depends(get_db)):
    _get_post_or_404(db, post_id)
    return schemas.PostMembersResponse(message="", members=_all_members(db, post_id))


@router.delete("/{post_id}/members/{user_id}", response_model=schemas.MessageResponse)
def remove_member(
    post_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = _get_owned_post_or_404(db, post_id, current_user)
    member = (
        db.query(models.PostMember)
        .filter(models.PostMember.post_id == post_id, models.PostMember.user_id == user_id)
        .first()
    )
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

    db.delete(member)
    db.commit()
    return schemas.MessageResponse(message="Member removed")


# ==========================================================================
# Add Music
# ==========================================================================

@router.put("/{post_id}/music", response_model=schemas.MusicResponse)
def set_music(
    post_id: int,
    payload: schemas.MusicUpdateRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = _get_owned_post_or_404(db, post_id, current_user)

    post.music_title = payload.title
    post.music_artist = payload.artist
    post.music_url = payload.audio_url
    post.music_start_seconds = payload.start_seconds
    db.commit()

    return schemas.MusicResponse(
        message="Music added",
        music=schemas.MusicOut(
            title=post.music_title,
            artist=post.music_artist,
            audio_url=post.music_url,
            start_seconds=post.music_start_seconds or 0,
        ),
    )


@router.delete("/{post_id}/music", response_model=schemas.MessageResponse)
def remove_music(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = _get_owned_post_or_404(db, post_id, current_user)
    post.music_title = None
    post.music_artist = None
    post.music_url = None
    post.music_start_seconds = None
    db.commit()
    return schemas.MessageResponse(message="Music removed")


# ==========================================================================
# Add Location
# ==========================================================================

@router.put("/{post_id}/location", response_model=schemas.LocationResponse)
def set_location(
    post_id: int,
    payload: schemas.LocationUpdateRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = _get_owned_post_or_404(db, post_id, current_user)

    post.location_name = payload.name
    post.location_latitude = payload.latitude
    post.location_longitude = payload.longitude
    db.commit()

    return schemas.LocationResponse(
        message="Location added",
        location=schemas.LocationOut(
            name=post.location_name,
            latitude=post.location_latitude,
            longitude=post.location_longitude,
        ),
    )


@router.delete("/{post_id}/location", response_model=schemas.MessageResponse)
def remove_location(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = _get_owned_post_or_404(db, post_id, current_user)
    post.location_name = None
    post.location_latitude = None
    post.location_longitude = None
    db.commit()
    return schemas.MessageResponse(message="Location removed")
