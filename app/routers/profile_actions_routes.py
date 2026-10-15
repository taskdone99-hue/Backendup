"""Profile contact, share-profile, and social-link APIs."""
import os
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, HttpUrl, field_validator
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user, get_current_user_optional
from app.database import get_db

router = APIRouter(prefix="/api", tags=["profile-actions"])


class SocialLinkIn(BaseModel):
    platform: str = Field(min_length=1, max_length=40)
    url: HttpUrl

    @field_validator("platform")
    @classmethod
    def normalize_platform(cls, value: str) -> str:
        value = value.strip().lower().replace(" ", "_")
        if not value or not all(c.isalnum() or c in "_-" for c in value):
            raise ValueError("platform may contain letters, numbers, underscores, and hyphens")
        return value


class SocialLinksUpdate(BaseModel):
    links: list[SocialLinkIn] = Field(max_length=20)


class SocialLinkOut(BaseModel):
    id: int
    platform: str
    url: str

    class Config:
        from_attributes = True


def _user_or_404(db: Session, user_id: int) -> models.User:
    user = db.query(models.User).filter(models.User.id == user_id, models.User.is_active.is_(True)).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _profile_url(user: models.User) -> str:
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if base:
        return f"{base}/u/{quote(user.username, safe='')}"
    return f"/u/{quote(user.username, safe='')}"


@router.get("/users/{user_id}/share-profile")
def share_profile(user_id: int, db: Session = Depends(get_db), current_user: models.User | None = Depends(get_current_user_optional)):
    user = _user_or_404(db, user_id)
    followers = db.query(models.Follow).filter(models.Follow.following_id == user.id).count()
    following = db.query(models.Follow).filter(models.Follow.follower_id == user.id).count()
    posts = db.query(models.Post).filter(models.Post.user_id == user.id).count()
    return {"user_id": user.id, "username": user.username, "display_name": user.full_name or user.username,
            "profile_picture": user.avatar_url, "profile_url": _profile_url(user),
            "followers_count": followers, "following_count": following, "posts_count": posts,
            "share_text": f"Check out @{user.username}'s profile"}


@router.get("/users/{user_id}/contact")
def profile_contact(user_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    user = _user_or_404(db, user_id)
    # Account login credentials are private: only the profile owner can retrieve them.
    is_owner = current_user.id == user.id
    return {"user_id": user.id, "username": user.username,
            "account_type": user.account_type.value if user.account_type else "personal",
            "business_name": user.business_name, "business_category": user.business_category,
            "business_description": user.business_description,
            "email": user.email if is_owner else None,
            "phone_number": user.phone_number if is_owner else None,
            "can_view_email": bool(is_owner and user.email),
            "can_view_phone": bool(is_owner and user.phone_number)}


@router.get("/users/{user_id}/social-links", response_model=list[SocialLinkOut])
def get_social_links(user_id: int, db: Session = Depends(get_db), current_user: models.User | None = Depends(get_current_user_optional)):
    user = _user_or_404(db, user_id)
    if user.is_private and (current_user is None or current_user.id != user.id) and not db.query(models.Follow).filter(models.Follow.follower_id == (current_user.id if current_user else -1), models.Follow.following_id == user.id).first():
        raise HTTPException(status_code=403, detail="This account is private")
    return db.query(models.UserSocialLink).filter(models.UserSocialLink.user_id == user.id).order_by(models.UserSocialLink.platform.asc()).all()


@router.put("/users/me/social-links", response_model=list[SocialLinkOut])
def replace_my_social_links(payload: SocialLinksUpdate, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    platforms = [link.platform for link in payload.links]
    if len(platforms) != len(set(platforms)):
        raise HTTPException(status_code=400, detail="Each platform can appear only once")
    db.query(models.UserSocialLink).filter(models.UserSocialLink.user_id == current_user.id).delete(synchronize_session=False)
    rows = [models.UserSocialLink(user_id=current_user.id, platform=link.platform, url=str(link.url)) for link in payload.links]
    db.add_all(rows)
    db.commit()
    for row in rows:
        db.refresh(row)
    return rows


@router.post("/users/me/social-links", response_model=SocialLinkOut, status_code=status.HTTP_201_CREATED)
def add_or_update_social_link(payload: SocialLinkIn, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    row = db.query(models.UserSocialLink).filter(models.UserSocialLink.user_id == current_user.id, models.UserSocialLink.platform == payload.platform).first()
    if row is None:
        row = models.UserSocialLink(user_id=current_user.id, platform=payload.platform, url=str(payload.url))
        db.add(row)
    else:
        row.url = str(payload.url)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/users/me/social-links/{platform}")
def delete_social_link(platform: str, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    row = db.query(models.UserSocialLink).filter(models.UserSocialLink.user_id == current_user.id, models.UserSocialLink.platform == platform.strip().lower().replace(" ", "_")).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Social link not found")
    db.delete(row)
    db.commit()
    return {"message": "Social link deleted", "platform": row.platform}
