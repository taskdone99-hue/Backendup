import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user
from app.services.media_service import delete_media_file, save_upload_file
from app.services.push_service import send_push
from app.services.location_service import resolve_location_from_form
from app.services import story_extras_service
from app.services.privacy_service import blocked_user_ids, muted_user_ids, is_blocked, is_close_friend


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
            joinedload(models.Story.mentions).joinedload(models.StoryMention.user),
            joinedload(models.Story.poll).joinedload(models.StoryPoll.options).joinedload(
                models.StoryPollOption.votes
            ),
            joinedload(models.Story.question).joinedload(models.StoryQuestion.responses),
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
    out.close_friends_only = story.visibility == models.StoryVisibility.close_friends

    out.views_count = len(story.views)
    out.reactions_count = len(story.reactions)

    if story.location_id and story.location is not None:
        out.location = schemas.LocationOut.model_validate(story.location)

    out.mentions = [
        schemas.StoryMentionOut(
            id=m.id, user=schemas.UserSummaryOut.model_validate(m.user), created_at=m.created_at
        )
        for m in story.mentions
    ]
    if story.poll is not None:
        out.poll = story_extras_service.to_poll_out(story.poll, viewer_id)
    if story.question is not None:
        out.question = story_extras_service.to_question_out(story.question)

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


def _viewer_can_see_story(db: Session, story: "models.Story", viewer_id: int) -> bool:
    """False only for a close_friends-only story viewed by someone who
    isn't the owner and isn't on the owner's Close Friends list."""
    if story.visibility != models.StoryVisibility.close_friends:
        return True
    if story.user_id == viewer_id:
        return True
    return is_close_friend(db, story.user_id, viewer_id)


def _get_active_story_or_404(
    db: Session,
    story_id: int,
    viewer_id: int | None = None,
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

    # Same "pretend it doesn't exist" treatment as a blocked user's content
    # (see is_blocked usage elsewhere in this file) — a close_friends-only
    # story is invisible, not "forbidden", to someone outside the list.
    if viewer_id is not None and not _viewer_can_see_story(db, story, viewer_id):
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

    mention_user_ids: str | None = Form(
        default=None, description="Comma-separated user ids to tag, e.g. '12,15'"
    ),
    poll_question: str | None = Form(default=None),
    poll_option_1: str | None = Form(default=None),
    poll_option_2: str | None = Form(default=None),
    question_prompt: str | None = Form(
        default=None, description="Adds an 'Ask me anything'-style question sticker"
    ),
    close_friends_only: bool = Form(
        default=False, description="If true, only visible to users on your Close Friends list"
    ),

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
    # Parse mentions / poll
    # ---------------------------------------------------------------

    parsed_mention_ids: list[int] = []
    if mention_user_ids and mention_user_ids.strip():
        try:
            parsed_mention_ids = [
                int(x.strip()) for x in mention_user_ids.split(",") if x.strip()
            ]
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="mention_user_ids must be comma-separated integers, e.g. '12,15'",
            )
        existing_count = (
            db.query(models.User.id).filter(models.User.id.in_(parsed_mention_ids)).count()
        )
        if existing_count != len(set(parsed_mention_ids)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="One or more mentioned users not found")

    poll_options = [o.strip() for o in (poll_option_1, poll_option_2) if o and o.strip()]
    if poll_question and len(poll_options) != 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A poll needs poll_question plus exactly two options (poll_option_1, poll_option_2)",
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
        visibility=(
            models.StoryVisibility.close_friends
            if close_friends_only
            else models.StoryVisibility.public
        ),
    )

    db.add(story)
    db.flush()

    if parsed_mention_ids:
        story_extras_service.attach_mentions(db, story, parsed_mention_ids)
    if poll_question and len(poll_options) == 2:
        story_extras_service.attach_poll(db, story, poll_question, poll_options)
    if question_prompt and question_prompt.strip():
        story_extras_service.attach_question(db, story, question_prompt.strip())

    db.commit()
    db.refresh(story)

    for mentioned_id in parsed_mention_ids:
        mentioned_user = db.query(models.User).filter(models.User.id == mentioned_id).first()
        if mentioned_user is not None:
            _notify_story_owner(
                db,
                owner_id=mentioned_id,
                actor=current_user,
                notif_type=models.NotificationType.mention,
                message=f"{current_user.username} mentioned you in their story",
                story_id=story.id,
            )

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

    muted_ids = set(muted_user_ids(db, current_user.id, for_stories=True))
    following_ids = [uid for uid in following_ids if uid not in muted_ids]

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

    # A close_friends-only story only belongs in this feed if the viewer is
    # actually on that author's Close Friends list.
    stories = [s for s in stories if _viewer_can_see_story(db, s, current_user.id)]

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
# Story Archive
# -------------------------------------------------------------------
#
# Every story the owner has ever posted stays queryable here after it
# expires out of the public feed/viewers/mine endpoints (see
# _active_story_query, which all of those use and which excludes anything
# past expires_at). cleanup_expired_stories.py no longer hard-deletes a
# story the moment it expires — see that file's ARCHIVE_RETENTION_DAYS —
# specifically so this endpoint has something to return.

@router.get(
    "/archive",
    response_model=schemas.PaginatedStoryResponse,
)
def get_story_archive(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Owner-only — the logged-in user's own expired stories, newest first."""
    now = datetime.now(timezone.utc)

    query = (
        db.query(models.Story)
        .options(
            joinedload(models.Story.user),
            joinedload(models.Story.location),
        )
        .filter(
            models.Story.user_id == current_user.id,
            models.Story.expires_at <= now,
        )
    )
    total = query.count()
    stories = query.order_by(models.Story.created_at.desc()).offset(offset).limit(limit).all()

    return schemas.PaginatedStoryResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[_to_story_out(s, current_user.id) for s in stories],
    )


# -------------------------------------------------------------------
# Story Drafts
# -------------------------------------------------------------------
#
# A draft is "finish this later" — saved media/caption/location, no
# expires_at, never shown in the feed/mine/archive/viewers endpoints.
# Publishing turns it into a real Story row (same expires_at logic as
# POST /api/stories) and deletes the draft.

def _to_draft_out(draft: models.StoryDraft) -> schemas.StoryDraftOut:
    out = schemas.StoryDraftOut.model_validate(draft)
    if draft.location_id and draft.location is not None:
        out.location = schemas.LocationOut.model_validate(draft.location)
    elif draft.location_name:
        out.location = schemas.LocationOut(
            name=draft.location_name,
            latitude=draft.location_latitude,
            longitude=draft.location_longitude,
        )
    return out


def _get_own_draft_or_404(db: Session, draft_id: int, current_user: models.User) -> models.StoryDraft:
    draft = (
        db.query(models.StoryDraft)
        .filter(models.StoryDraft.id == draft_id, models.StoryDraft.user_id == current_user.id)
        .first()
    )
    if draft is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found")
    return draft


@router.post("/drafts", response_model=schemas.StoryDraftOut, status_code=status.HTTP_201_CREATED)
def create_story_draft(
    file: UploadFile,
    caption: str | None = Form(default=None),
    location_id: str | None = Form(default=None),
    location_name: str | None = Form(default=None),
    location_address: str | None = Form(default=None),
    location_city: str | None = Form(default=None),
    location_state: str | None = Form(default=None),
    location_country: str | None = Form(default=None),
    location_latitude: str | None = Form(default=None),
    location_longitude: str | None = Form(default=None),
    location_place_id: str | None = Form(default=None),
    close_friends_only: bool = Form(default=False),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    parsed_location_id = _optional_int(location_id, "location_id")
    parsed_location_latitude = _optional_float(location_latitude, "location_latitude")
    parsed_location_longitude = _optional_float(location_longitude, "location_longitude")

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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    url, kind = save_upload_file(file, "stories", allow_video=True)

    draft = models.StoryDraft(
        user_id=current_user.id,
        media_url=url,
        media_type=models.MediaType.video if kind == "video" else models.MediaType.image,
        caption=caption,
        location_name=location.name if location else location_name,
        location_latitude=location.latitude if location else parsed_location_latitude,
        location_longitude=location.longitude if location else parsed_location_longitude,
        location_id=location.id if location else None,
        close_friends_only=close_friends_only,
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return _to_draft_out(draft)


@router.get("/drafts", response_model=schemas.PaginatedStoryDraftsResponse)
def get_story_drafts(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    query = db.query(models.StoryDraft).filter(models.StoryDraft.user_id == current_user.id)
    total = query.count()
    drafts = query.order_by(models.StoryDraft.updated_at.desc()).offset(offset).limit(limit).all()
    return schemas.PaginatedStoryDraftsResponse(
        total=total, limit=limit, offset=offset, items=[_to_draft_out(d) for d in drafts]
    )


@router.get("/drafts/{draft_id}", response_model=schemas.StoryDraftOut)
def get_story_draft(
    draft_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    draft = _get_own_draft_or_404(db, draft_id, current_user)
    return _to_draft_out(draft)


@router.delete("/drafts/{draft_id}", response_model=schemas.MessageResponse)
def delete_story_draft(
    draft_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    draft = _get_own_draft_or_404(db, draft_id, current_user)
    delete_media_file(draft.media_url)
    db.delete(draft)
    db.commit()
    return schemas.MessageResponse(message="Draft deleted")


@router.post("/drafts/{draft_id}/publish", response_model=schemas.StoryOut, status_code=status.HTTP_201_CREATED)
def publish_story_draft(
    draft_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Turns the draft into a live story (starts its 24h expiry clock now)
    and deletes the draft row — same one-way conversion as posting fresh,
    just skipping the re-upload."""
    draft = _get_own_draft_or_404(db, draft_id, current_user)

    story = models.Story(
        user_id=current_user.id,
        media_url=draft.media_url,
        media_type=draft.media_type,
        caption=draft.caption,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=STORY_LIFETIME_HOURS),
        # Story only stores location_id (no flat name/lat/lng columns,
        # unlike StoryDraft/Reel/Post) — the draft's flat fields were just
        # a fallback for "resolve_location_from_form couldn't attach a
        # Location row", which has nothing to carry over here.
        location_id=draft.location_id,
        visibility=(
            models.StoryVisibility.close_friends
            if draft.close_friends_only
            else models.StoryVisibility.public
        ),
    )
    db.add(story)
    db.delete(draft)
    db.commit()
    db.refresh(story)

    story = _active_story_query(db).filter(models.Story.id == story.id).first()
    return _to_story_out(story, current_user.id)


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
        current_user.id,
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
        current_user.id,
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
        current_user.id,
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
        current_user.id,
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
        current_user.id,
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
        current_user.id,
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
        current_user.id,
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


# -------------------------------------------------------------------
# Poll / Question stickers
# -------------------------------------------------------------------

@router.post("/{story_id}/poll/vote", response_model=schemas.StoryPollOut)
def vote_story_poll(
    story_id: int,
    payload: schemas.StoryPollVoteRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Casts (or changes) this viewer's vote. One vote per user per poll —
    voting again with a different option_id just moves it."""
    story = _get_active_story_or_404(db, story_id, current_user.id)
    if story.poll is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This story has no poll")

    option = next((o for o in story.poll.options if o.id == payload.option_id), None)
    if option is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Poll option not found")

    existing_vote = (
        db.query(models.StoryPollVote)
        .filter(
            models.StoryPollVote.poll_id == story.poll.id,
            models.StoryPollVote.user_id == current_user.id,
        )
        .first()
    )
    if existing_vote is not None:
        existing_vote.option_id = option.id
    else:
        db.add(models.StoryPollVote(poll_id=story.poll.id, option_id=option.id, user_id=current_user.id))
    db.commit()

    story = _get_active_story_or_404(db, story_id, current_user.id)
    return story_extras_service.to_poll_out(story.poll, current_user.id)


@router.post(
    "/{story_id}/question/respond",
    response_model=schemas.StoryQuestionResponseOut,
    status_code=status.HTTP_201_CREATED,
)
def respond_to_story_question(
    story_id: int,
    payload: schemas.StoryQuestionResponseIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    story = _get_active_story_or_404(db, story_id, current_user.id)
    if story.question is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This story has no question")
    if is_blocked(db, current_user.id, story.user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Story not found")

    response = models.StoryQuestionResponse(
        question_id=story.question.id, user_id=current_user.id, response_text=payload.response_text
    )
    db.add(response)
    db.commit()
    db.refresh(response)

    _notify_story_owner(
        db,
        owner_id=story.user_id,
        actor=current_user,
        notif_type=models.NotificationType.other,
        message=f"{current_user.username} answered your question: {payload.response_text[:80]}",
        story_id=story.id,
    )
    return schemas.StoryQuestionResponseOut(
        id=response.id,
        user=schemas.UserSummaryOut.model_validate(current_user),
        response_text=response.response_text,
        created_at=response.created_at,
    )


@router.get(
    "/{story_id}/question/responses",
    response_model=schemas.StoryQuestionResponsesResponse,
)
def get_story_question_responses(
    story_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Story-owner only — same as Instagram, where question-sticker
    answers are private to the poster, not shown publicly like poll votes."""
    story = _get_active_story_or_404(db, story_id, current_user.id)
    _require_own_story(story, current_user)
    if story.question is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This story has no question")

    return schemas.StoryQuestionResponsesResponse(
        prompt=story.question.prompt,
        items=[
            schemas.StoryQuestionResponseOut(
                id=r.id,
                user=schemas.UserSummaryOut.model_validate(r.user),
                response_text=r.response_text,
                created_at=r.created_at,
            )
            for r in sorted(story.question.responses, key=lambda r: r.created_at)
        ],
    )
