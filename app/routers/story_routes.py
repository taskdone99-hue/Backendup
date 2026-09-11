import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user
from app.services.media_service import delete_media_file, save_upload_file
from app.services.push_service import send_push
from app.services.location_service import resolve_location_from_form


router = APIRouter(prefix="/api/stories", tags=["stories"])

STORY_LIFETIME_HOURS = int(os.getenv("STORY_LIFETIME_HOURS", "24"))


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def _active_story_query(db: Session):
    now = datetime.now(timezone.utc)

    return (
        db.query(models.Story)
        .options(
            joinedload(models.Story.user),
            joinedload(models.Story.location),
        )
        .filter(models.Story.expires_at > now)
    )


def _optional_int(value: str | None, field_name: str) -> int | None:
    """
    Swagger multipart/form-data sends empty fields as "".
    Convert "" -> None and validate actual integer values.
    """
    if value is None or not value.strip():
        return None

    try:
        return int(value.strip())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be a valid integer",
        )


def _optional_float(value: str | None, field_name: str) -> float | None:
    """
    Swagger multipart/form-data sends empty fields as "".
    Convert "" -> None and validate actual numeric values.
    """
    if value is None or not value.strip():
        return None

    try:
        return float(value.strip())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be a valid number",
        )


def _to_story_out(
    story: models.Story,
    viewer_id: int | None,
) -> schemas.StoryOut:

    out = schemas.StoryOut.model_validate(story)

    out.user = schemas.UserSummaryOut.model_validate(story.user)

    out.views_count = len(story.views)
    out.reactions_count = len(story.reactions)

    if story.location_id and story.location is not None:
        out.location = schemas.LocationOut.model_validate(story.location)

    if viewer_id is not None:
        out.viewed_by_me = any(
            v.viewer_id == viewer_id
            for v in story.views
        )

        my_reaction = next(
            (
                r
                for r in story.reactions
                if r.user_id == viewer_id
            ),
            None,
        )

        out.my_reaction = (
            my_reaction.emoji
            if my_reaction
            else None
        )

    return out


def _get_active_story_or_404(
    db: Session,
    story_id: int,
) -> models.Story:

    story = (
        _active_story_query(db)
        .filter(models.Story.id == story_id)
        .first()
    )

    if story is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Story not found",
        )

    return story


def _require_own_story(
    story: models.Story,
    current_user: models.User,
) -> None:

    if story.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only do this on your own story",
        )


def _get_or_create_direct_conversation(
    db: Session,
    user_a_id: int,
    user_b_id: int,
) -> models.Conversation:

    """
    Same reuse-existing-1:1-thread logic as
    POST /api/chat/conversations.
    """

    my_conversation_ids = select(
        models.ConversationParticipant.conversation_id
    ).where(
        models.ConversationParticipant.user_id == user_a_id
    )

    candidates = (
        db.query(models.Conversation)
        .filter(
            models.Conversation.is_group.is_(False),
            models.Conversation.id.in_(my_conversation_ids),
        )
        .all()
    )

    for conv in candidates:
        participant_ids = {
            p.user_id
            for p in conv.participants
        }

        if participant_ids == {user_a_id, user_b_id}:
            return conv

    conversation = models.Conversation(
        is_group=False,
        title=None,
    )

    db.add(conversation)
    db.flush()

    for uid in {user_a_id, user_b_id}:
        db.add(
            models.ConversationParticipant(
                conversation_id=conversation.id,
                user_id=uid,
            )
        )

    db.commit()
    db.refresh(conversation)

    return conversation


def _notify_story_owner(
    db: Session,
    *,
    owner_id: int,
    actor: models.User,
    notif_type: models.NotificationType,
    message: str,
    story_id: int,
) -> None:

    """
    Best-effort notification + push to story owner.
    No-op if actor == owner.
    """

    if actor.id == owner_id:
        return

    db.add(
        models.Notification(
            user_id=owner_id,
            actor_id=actor.id,
            type=notif_type,
            message=message,
            target_type="story",
            target_id=story_id,
        )
    )

    db.commit()

    tokens = (
        db.query(models.DeviceToken.token)
        .filter(
            models.DeviceToken.user_id == owner_id
        )
        .all()
    )

    token_list = [t[0] for t in tokens]

    if token_list:
        send_push(
            token_list,
            title=actor.username,
            body=message,
            data={
                "type": "story",
                "story_id": str(story_id),
            },
        )


# -------------------------------------------------------------------
# Create Story
# -------------------------------------------------------------------

@router.post(
    "",
    response_model=schemas.StoryOut,
    status_code=status.HTTP_201_CREATED,
)
def create_story(
    file: UploadFile,
    caption: str | None = Form(default=None),

    # IMPORTANT:
    # Receive multipart values as strings because Swagger sends
    # empty optional fields as "".
    location_id: str | None = Form(
        default=None,
        description=(
            "Attach an already-saved location "
            "(see POST /api/locations) by id"
        ),
    ),

    location_name: str | None = Form(default=None),
    location_address: str | None = Form(default=None),
    location_city: str | None = Form(default=None),
    location_state: str | None = Form(default=None),
    location_country: str | None = Form(default=None),

    location_latitude: str | None = Form(default=None),
    location_longitude: str | None = Form(default=None),

    location_place_id: str | None = Form(default=None),

    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):

    # ---------------------------------------------------------------
    # Convert optional multipart values
    # ---------------------------------------------------------------

    parsed_location_id = _optional_int(
        location_id,
        "location_id",
    )

    parsed_location_latitude = _optional_float(
        location_latitude,
        "location_latitude",
    )

    parsed_location_longitude = _optional_float(
        location_longitude,
        "location_longitude",
    )

    # ---------------------------------------------------------------
    # Validate coordinates
    # ---------------------------------------------------------------

    if parsed_location_latitude is not None:

        if not (
            -90
            <= parsed_location_latitude
            <= 90
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "location_latitude must be "
                    "between -90 and 90"
                ),
            )

    if parsed_location_longitude is not None:

        if not (
            -180
            <= parsed_location_longitude
            <= 180
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "location_longitude must be "
                    "between -180 and 180"
                ),
            )

    # ---------------------------------------------------------------
    # Resolve / create location
    # ---------------------------------------------------------------

    try:

        location = resolve_location_from_form(
            db,
            location_id=parsed_location_id,
            location_name=location_name,
            location_address=location_address,
            location_city=location_city,
            location_state=location_state,
            location_country=location_country,
            location_latitude=parsed_location_latitude,
            location_longitude=parsed_location_longitude,
            location_place_id=location_place_id,
        )

    except ValueError as e:

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )

    # ---------------------------------------------------------------
    # Save uploaded story media
    # ---------------------------------------------------------------

    url, kind = save_upload_file(
        file,
        "stories",
        allow_video=True,
    )

    # ---------------------------------------------------------------
    # Create story
    # ---------------------------------------------------------------

    story = models.Story(
        user_id=current_user.id,
        media_url=url,
        media_type=(
            models.MediaType.video
            if kind == "video"
            else models.MediaType.image
        ),
        caption=caption,
        expires_at=(
            datetime.now(timezone.utc)
            + timedelta(hours=STORY_LIFETIME_HOURS)
        ),
        location_id=(
            location.id
            if location
            else None
        ),
    )

    db.add(story)
    db.commit()
    db.refresh(story)

    # Load relationships before serialization
    story = (
        _active_story_query(db)
        .filter(models.Story.id == story.id)
        .first()
    )

    return _to_story_out(
        story,
        current_user.id,
    )


# -------------------------------------------------------------------
# Story Feed
# -------------------------------------------------------------------

@router.get(
    "/feed",
    response_model=schemas.StoryFeedResponse,
)
def get_story_feed(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Active (non-expired) stories from users the current
    user follows — NOT their own.
    """

    following_ids = [
        row[0]
        for row in (
            db.query(models.Follow.following_id)
            .filter(
                models.Follow.follower_id
                == current_user.id
            )
            .all()
        )
    ]

    if not following_ids:
        return schemas.StoryFeedResponse(
            items=[]
        )

    stories = (
        _active_story_query(db)
        .filter(
            models.Story.user_id.in_(following_ids)
        )
        .order_by(
            models.Story.created_at.desc()
        )
        .all()
    )

    grouped: dict[
        int,
        list[models.Story]
    ] = {}

    for story in stories:
        grouped.setdefault(
            story.user_id,
            [],
        ).append(story)

    items = []

    for uid, user_stories in grouped.items():

        user = user_stories[0].user

        story_outs = [
            _to_story_out(
                s,
                current_user.id,
            )
            for s in user_stories
        ]

        has_unseen = any(
            not s.viewed_by_me
            for s in story_outs
        )

        user_summary = (
            schemas.UserSummaryOut
            .model_validate(user)
        )

        user_summary.is_following = True

        items.append(
            schemas.StoryUserFeedOut(
                user=user_summary,
                stories=story_outs,
                has_unseen=has_unseen,
            )
        )

    # Unseen stories first,
    # then most recently active.
    items.sort(
        key=lambda entry: (
            not entry.has_unseen,
            -entry.stories[0]
            .created_at
            .timestamp(),
        )
    )

    return schemas.StoryFeedResponse(
        items=items
    )


# -------------------------------------------------------------------
# My Stories
# -------------------------------------------------------------------

@router.get(
    "/mine",
    response_model=schemas.MyStoriesResponse,
)
def get_my_stories(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Logged-in user's own active stories,
    newest first.
    """

    stories = (
        _active_story_query(db)
        .filter(
            models.Story.user_id
            == current_user.id
        )
        .order_by(
            models.Story.created_at.desc()
        )
        .all()
    )

    return schemas.MyStoriesResponse(
        items=[
            _to_story_out(
                s,
                current_user.id,
            )
            for s in stories
        ]
    )


# -------------------------------------------------------------------
# Get Single Story
# -------------------------------------------------------------------

@router.get(
    "/{story_id}",
    response_model=schemas.StoryOut,
)
def get_story(
    story_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):

    story = _get_active_story_or_404(
        db,
        story_id,
    )

    return _to_story_out(
        story,
        current_user.id,
    )


# -------------------------------------------------------------------
# Delete Story
# -------------------------------------------------------------------

@router.delete(
    "/{story_id}",
    response_model=schemas.MessageResponse,
)
def delete_story(
    story_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):

    story = (
        db.query(models.Story)
        .filter(
            models.Story.id == story_id
        )
        .first()
    )

    if story is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Story not found",
        )

    if story.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You can only delete "
                "your own story"
            ),
        )

    delete_media_file(
        story.media_url
    )

    db.delete(story)
    db.commit()

    return schemas.MessageResponse(
        message="Story deleted"
    )


# -------------------------------------------------------------------
# Story View
# -------------------------------------------------------------------

@router.post(
    "/{story_id}/view",
    response_model=schemas.StoryViewResponse,
)
def view_story(
    story_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):

    story = _get_active_story_or_404(
        db,
        story_id,
    )

    existing = (
        db.query(models.StoryView)
        .filter(
            models.StoryView.story_id
            == story.id,
            models.StoryView.viewer_id
            == current_user.id,
        )
        .first()
    )

    if existing is None:

        db.add(
            models.StoryView(
                story_id=story.id,
                viewer_id=current_user.id,
            )
        )

        db.commit()

    views_count = (
        db.query(models.StoryView)
        .filter(
            models.StoryView.story_id
            == story.id
        )
        .count()
    )

    return schemas.StoryViewResponse(
        message="View recorded",
        views_count=views_count,
    )


# -------------------------------------------------------------------
# Story Viewers
# -------------------------------------------------------------------

@router.get(
    "/{story_id}/viewers",
    response_model=schemas.StoryViewersResponse,
)
def get_story_viewers(
    story_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):

    story = _get_active_story_or_404(
        db,
        story_id,
    )

    _require_own_story(
        story,
        current_user,
    )

    views = (
        db.query(models.StoryView)
        .options(
            joinedload(
                models.StoryView.viewer
            )
        )
        .filter(
            models.StoryView.story_id
            == story_id
        )
        .order_by(
            models.StoryView.viewed_at.desc()
        )
        .all()
    )

    items = [
        schemas.StoryViewerOut(
            id=v.viewer.id,
            user_id=v.viewer.id,
            username=v.viewer.username,
            full_name=v.viewer.full_name,
            avatar_url=v.viewer.avatar_url,
            viewed_at=v.viewed_at,
        )
        for v in views
    ]

    return schemas.StoryViewersResponse(
        views_count=len(items),
        items=items,
    )


# -------------------------------------------------------------------
# Story Reactions
# -------------------------------------------------------------------

@router.post(
    "/{story_id}/react",
    response_model=schemas.StoryOut,
)
def react_to_story(
    story_id: int,
    payload: schemas.StoryReactionCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):

    story = _get_active_story_or_404(
        db,
        story_id,
    )

    existing = (
        db.query(models.StoryReaction)
        .filter(
            models.StoryReaction.story_id
            == story_id,
            models.StoryReaction.user_id
            == current_user.id,
        )
        .first()
    )

    if existing is not None:

        existing.emoji = payload.emoji

    else:

        db.add(
            models.StoryReaction(
                story_id=story_id,
                user_id=current_user.id,
                emoji=payload.emoji,
            )
        )

    db.commit()
    db.refresh(story)

    _notify_story_owner(
        db,
        owner_id=story.user_id,
        actor=current_user,
        notif_type=models.NotificationType.like,
        message=(
            f"{current_user.username} "
            f"reacted {payload.emoji} "
            f"to your story"
        ),
        story_id=story.id,
    )

    return _to_story_out(
        story,
        current_user.id,
    )


# -------------------------------------------------------------------
# Remove Story Reaction
# -------------------------------------------------------------------

@router.delete(
    "/{story_id}/react",
    response_model=schemas.StoryOut,
)
def remove_story_reaction(
    story_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):

    story = _get_active_story_or_404(
        db,
        story_id,
    )

    existing = (
        db.query(models.StoryReaction)
        .filter(
            models.StoryReaction.story_id
            == story_id,
            models.StoryReaction.user_id
            == current_user.id,
        )
        .first()
    )

    if existing is not None:

        db.delete(existing)
        db.commit()
        db.refresh(story)

    return _to_story_out(
        story,
        current_user.id,
    )


# -------------------------------------------------------------------
# Story Reactions List
# -------------------------------------------------------------------

@router.get(
    "/{story_id}/reactions",
    response_model=schemas.StoryReactionsResponse,
)
def get_story_reactions(
    story_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):

    story = _get_active_story_or_404(
        db,
        story_id,
    )

    _require_own_story(
        story,
        current_user,
    )

    reactions = (
        db.query(models.StoryReaction)
        .filter(
            models.StoryReaction.story_id
            == story_id
        )
        .order_by(
            models.StoryReaction.created_at.desc()
        )
        .all()
    )

    items = [
        schemas.StoryReactorOut(
            id=r.user.id,
            username=r.user.username,
            avatar_url=r.user.avatar_url,
            emoji=r.emoji,
            created_at=r.created_at,
        )
        for r in reactions
    ]

    return schemas.StoryReactionsResponse(
        reactions_count=len(items),
        items=items,
    )


# -------------------------------------------------------------------
# Story Reply
# -------------------------------------------------------------------

@router.post(
    "/{story_id}/reply",
    response_model=schemas.MessageOut,
    status_code=status.HTTP_201_CREATED,
)
def reply_to_story(
    story_id: int,
    payload: schemas.StoryReplyCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Replying to a story sends a DM to its owner
    in their 1:1 conversation, tagged with the story.
    """

    story = _get_active_story_or_404(
        db,
        story_id,
    )

    if story.user_id == current_user.id:

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "You can't reply "
                "to your own story"
            ),
        )

    conversation = _get_or_create_direct_conversation(
        db,
        current_user.id,
        story.user_id,
    )

    message = models.Message(
        conversation_id=conversation.id,
        sender_id=current_user.id,
        content=payload.content,
        reply_to_story_id=story.id,
    )

    db.add(message)
    db.commit()
    db.refresh(message)

    preview = (
        payload.content
        if len(payload.content) <= 80
        else payload.content[:77] + "..."
    )

    _notify_story_owner(
        db,
        owner_id=story.user_id,
        actor=current_user,
        notif_type=models.NotificationType.message,
        message=(
            f"{current_user.username} "
            f"replied to your story: "
            f"{preview}"
        ),
        story_id=story.id,
    )

    return message