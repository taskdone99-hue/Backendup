import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user
from app.services.media_service import delete_media_file, save_upload_file

router = APIRouter(prefix="/api/stories", tags=["stories"])

STORY_LIFETIME_HOURS = int(os.getenv("STORY_LIFETIME_HOURS", "24"))


def _active_story_query(db: Session):
    now = datetime.now(timezone.utc)
    return db.query(models.Story).filter(models.Story.expires_at > now)


def _to_story_out(story: models.Story, viewer_id: int | None) -> schemas.StoryOut:
    out = schemas.StoryOut.model_validate(story)
    out.views_count = len(story.views)
    if viewer_id is not None:
        out.viewed_by_me = any(v.viewer_id == viewer_id for v in story.views)
    return out


def _get_active_story_or_404(db: Session, story_id: int) -> models.Story:
    story = _active_story_query(db).filter(models.Story.id == story_id).first()
    if story is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Story not found")
    return story


# ---- 4. Stories ----

@router.post("", response_model=schemas.StoryOut, status_code=status.HTTP_201_CREATED)
def create_story(
    file: UploadFile,
    caption: str | None = Form(default=None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    url, kind = save_upload_file(file, "stories", allow_video=True)

    story = models.Story(
        user_id=current_user.id,
        media_url=url,
        media_type=models.MediaType.video if kind == "video" else models.MediaType.image,
        caption=caption,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=STORY_LIFETIME_HOURS),
    )
    db.add(story)
    db.commit()
    db.refresh(story)

    return _to_story_out(story, current_user.id)


@router.get("/feed", response_model=schemas.StoryFeedResponse)
def get_story_feed(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Active (non-expired) stories from users the current user follows, plus
    their own, grouped by author — newest author activity first, matching
    the usual "stories tray" ordering.
    """
    following_ids = [
        row[0]
        for row in db.query(models.Follow.following_id)
        .filter(models.Follow.follower_id == current_user.id)
        .all()
    ]
    author_ids = set(following_ids) | {current_user.id}

    stories = (
        _active_story_query(db)
        .filter(models.Story.user_id.in_(author_ids))
        .order_by(models.Story.created_at.desc())
        .all()
    )

    grouped: dict[int, list[models.Story]] = {}
    for story in stories:
        grouped.setdefault(story.user_id, []).append(story)

    items = []
    for uid, user_stories in grouped.items():
        user = user_stories[0].user
        story_outs = [_to_story_out(s, current_user.id) for s in user_stories]
        has_unseen = any(not s.viewed_by_me for s in story_outs)
        items.append(
            schemas.StoryUserFeedOut(
                user=schemas.UserSummaryOut.model_validate(user),
                stories=story_outs,
                has_unseen=has_unseen,
            )
        )

    # Accounts with unseen stories first, then most recently active.
    items.sort(
        key=lambda entry: (not entry.has_unseen, -entry.stories[0].created_at.timestamp())
    )

    return schemas.StoryFeedResponse(items=items)


@router.get("/{story_id}", response_model=schemas.StoryOut)
def get_story(
    story_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    story = _get_active_story_or_404(db, story_id)
    return _to_story_out(story, current_user.id)


@router.delete("/{story_id}", response_model=schemas.MessageResponse)
def delete_story(
    story_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    story = db.query(models.Story).filter(models.Story.id == story_id).first()
    if story is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Story not found")
    if story.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only delete your own story"
        )

    delete_media_file(story.media_url)
    db.delete(story)
    db.commit()

    return schemas.MessageResponse(message="Story deleted")


@router.post("/{story_id}/view", response_model=schemas.StoryViewResponse)
def view_story(
    story_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    story = _get_active_story_or_404(db, story_id)

    existing = (
        db.query(models.StoryView)
        .filter(
            models.StoryView.story_id == story.id,
            models.StoryView.viewer_id == current_user.id,
        )
        .first()
    )
    if existing is None:
        db.add(models.StoryView(story_id=story.id, viewer_id=current_user.id))
        db.commit()

    views_count = (
        db.query(models.StoryView).filter(models.StoryView.story_id == story.id).count()
    )
    return schemas.StoryViewResponse(message="View recorded", views_count=views_count)
