import logging
from datetime import datetime, timezone

from fastapi import (
    APIRouter, Depends, Form, HTTPException, Query, UploadFile,
    WebSocket, WebSocketDisconnect, status
)
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.database import get_db, SessionLocal
from app import models, schemas
from app.auth import get_current_user, get_user_from_raw_token
from app.ws_manager import manager
from app.services.notification_service import notify_user
from app.services.privacy_service import is_blocked, is_conversation_muted
from app.services.media_service import save_upload_file
from app.routers.content_routes import _require_author_visible

router = APIRouter(prefix="/api/chat", tags=["chat"])

logger = logging.getLogger(__name__)


# ---- internal helpers ----

def _get_conversation_or_404(db: Session, conversation_id: int) -> models.Conversation:
    conversation = (
        db.query(models.Conversation)
        .filter(models.Conversation.id == conversation_id)
        .first()
    )
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return conversation


def _get_participant_or_403(db: Session, conversation_id: int, user_id: int) -> models.ConversationParticipant:
    participant = (
        db.query(models.ConversationParticipant)
        .filter(
            models.ConversationParticipant.conversation_id == conversation_id,
            models.ConversationParticipant.user_id == user_id,
        )
        .first()
    )
    if participant is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You're not a participant in this conversation",
        )
    return participant


def _require_participant(db: Session, conversation_id: int, user_id: int) -> None:
    _get_participant_or_403(db, conversation_id, user_id)


def _conversation_participant_ids(db: Session, conversation_id: int) -> list[int]:
    rows = (
        db.query(models.ConversationParticipant.user_id)
        .filter(models.ConversationParticipant.conversation_id == conversation_id)
        .all()
    )
    return [r[0] for r in rows]


def _reject_if_blocked_in_conversation(db: Session, conversation_id: int, sender_id: int) -> None:
    """A block, in either direction, between the sender and any other
    participant stops the message — same rule as content_routes.is_blocked,
    applied here rather than at conversation-creation time so it also
    covers a block that happens *after* a thread already exists (an
    existing group chat with a later-blocked member, for instance)."""
    other_ids = [uid for uid in _conversation_participant_ids(db, conversation_id) if uid != sender_id]
    for uid in other_ids:
        if is_blocked(db, sender_id, uid):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Can't send messages in this conversation"
            )


def _validate_reply_target(
    db: Session, conversation_id: int, reply_to_message_id: int | None
) -> int | None:
    if reply_to_message_id is None:
        return None
    original = (
        db.query(models.Message)
        .filter(
            models.Message.id == reply_to_message_id,
            models.Message.conversation_id == conversation_id,
        )
        .first()
    )
    if original is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="reply_to_message_id must refer to a message in this conversation",
        )
    return reply_to_message_id


def _reel_visible_to(db: Session, reel: models.Reel, viewer_id: int | None) -> bool:
    """Same visibility rule as GET /api/reels/{id} (block either way, or a
    private account the viewer doesn't follow), as a yes/no."""
    try:
        _require_author_visible(db, reel.user, viewer_id)
    except HTTPException:
        return False
    return True


def _build_shared_reel_out(
    db: Session, reel_id: int, viewer_id: int | None
) -> schemas.SharedReelOut:
    """Preview for a reel shared in chat, resolved for one viewer. A deleted
    or not-visible-to-this-viewer reel comes back as is_available=False with
    no details, so a share can't expose a private account's reel to someone
    who couldn't open it directly. viewer_id=None is treated as a logged-out
    viewer (public accounts only)."""
    reel = db.query(models.Reel).filter(models.Reel.id == reel_id).first()
    if reel is None or not _reel_visible_to(db, reel, viewer_id):
        return schemas.SharedReelOut(reel_id=reel_id, is_available=False)
    return schemas.SharedReelOut(
        reel_id=reel.id,
        is_available=True,
        video_url=reel.video_url,
        thumbnail_url=reel.thumbnail_url,
        caption=reel.caption,
        duration_seconds=reel.duration_seconds,
        user=schemas.UserSummaryOut.model_validate(reel.user),
    )


def _get_shareable_reel_or_404(db: Session, reel_id: int, sender_id: int) -> models.Reel:
    reel = db.query(models.Reel).filter(models.Reel.id == reel_id).first()
    if reel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reel not found")
    # You can only share what you could open yourself (404 if blocked, 403
    # if private and not followed) — same responses as GET /api/reels/{id}.
    _require_author_visible(db, reel.user, sender_id)
    return reel


def _post_visible_to(db: Session, post: models.Post, viewer_id: int | None) -> bool:
    """Same visibility rule as GET /api/posts/{id} (block either way, or a
    private account the viewer doesn't follow), as a yes/no — mirrors
    _reel_visible_to above."""
    try:
        _require_author_visible(db, post.user, viewer_id)
    except HTTPException:
        return False
    return True


def _build_shared_post_out(
    db: Session, post_id: int, viewer_id: int | None
) -> schemas.SharedPostOut:
    """Preview for a post shared in chat, resolved for one viewer — same
    is_available fallback as _build_shared_reel_out, for the same reasons."""
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if post is None or not _post_visible_to(db, post, viewer_id):
        return schemas.SharedPostOut(post_id=post_id, is_available=False)
    return schemas.SharedPostOut(
        post_id=post.id,
        is_available=True,
        media_url=post.media_url,
        media_type=post.media_type,
        caption=post.caption,
        user=schemas.UserSummaryOut.model_validate(post.user),
    )


def _get_shareable_post_or_404(db: Session, post_id: int, sender_id: int) -> models.Post:
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if post is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    # Same visibility rule as reels above — you can only share what you
    # could open yourself.
    _require_author_visible(db, post.user, sender_id)
    return post


def _to_message_out(
    db: Session, message: models.Message, viewer_id: int | None = None
) -> schemas.MessageOut:
    """`viewer_id` only matters for shared-reel messages, whose preview depends
    on who's looking (see _build_shared_reel_out)."""
    out = schemas.MessageOut.model_validate(message)
    if message.is_deleted:
        out.content = "This message was deleted"
        out.media_url = None
        out.shared_reel_id = None
        out.shared_post_id = None
    elif message.shared_reel_id is not None:
        out.shared_reel = _build_shared_reel_out(db, message.shared_reel_id, viewer_id)
    elif message.shared_post_id is not None:
        out.shared_post = _build_shared_post_out(db, message.shared_post_id, viewer_id)

    if message.reply_to_message_id is not None:
        original = (
            db.query(models.Message)
            .filter(models.Message.id == message.reply_to_message_id)
            .first()
        )
        if original is not None:
            out.reply_to = schemas.MessageRepliedToOut(
                id=original.id,
                sender_id=original.sender_id,
                content="This message was deleted" if original.is_deleted else original.content,
                media_type=original.media_type,
                is_deleted=original.is_deleted,
                is_reel_share=original.shared_reel_id is not None and not original.is_deleted,
                is_post_share=original.shared_post_id is not None and not original.is_deleted,
            )

    out.reactions = [
        schemas.MessageReactionOut(user_id=r.user_id, emoji=r.emoji) for r in message.reactions
    ]

    # status: "sent" (default) until every recipient's row clears each bar.
    recipient_statuses = (
        db.query(models.MessageStatus).filter(models.MessageStatus.message_id == message.id).all()
    )
    if recipient_statuses:
        if all(s.read_at is not None for s in recipient_statuses):
            out.status = "read"
        elif all(s.delivered_at is not None for s in recipient_statuses):
            out.status = "delivered"
    return out


def _to_conversation_out(
    db: Session, conversation: models.Conversation, viewer_id: int | None = None
) -> schemas.ConversationOut:
    participants = [
        schemas.ChatParticipantOut(
            id=p.user.id,
            username=p.user.username,
            full_name=p.user.full_name,
            avatar_url=p.user.avatar_url,
            is_online=manager.is_online(p.user.id),
        )
        for p in conversation.participants
    ]
    last_message = (
        db.query(models.Message)
        .filter(models.Message.conversation_id == conversation.id)
        .order_by(models.Message.created_at.desc())
        .first()
    )

    unread_count = 0
    my_status = "accepted"
    is_muted = False
    if viewer_id is not None:
        my_participant = next((p for p in conversation.participants if p.user_id == viewer_id), None)
        if my_participant is not None:
            q = db.query(models.Message).filter(models.Message.conversation_id == conversation.id)
            if my_participant.last_read_message_id is not None:
                q = q.filter(models.Message.id > my_participant.last_read_message_id)
            unread_count = q.filter(models.Message.sender_id != viewer_id).count()
            my_status = my_participant.status.value
        is_muted = is_conversation_muted(db, viewer_id, conversation.id)

    return schemas.ConversationOut(
        id=conversation.id,
        is_group=conversation.is_group,
        title=conversation.title,
        created_at=conversation.created_at,
        participants=participants,
        last_message=_to_message_out(db, last_message, viewer_id) if last_message else None,
        unread_count=unread_count,
        status=my_status,
        is_muted=is_muted,
    )


def _build_intro_message(other_user: models.User) -> str:
    """The one-time auto-DM sent (as if from `other_user`) the first time
    someone starts a 1:1 conversation with them."""
    if other_user.account_type == models.AccountType.business:
        name = other_user.business_name or other_user.full_name or other_user.username
        text = f"Hi! \U0001F44B Welcome to {name}."
        if other_user.business_description:
            text += f" {other_user.business_description}"
        text += " Check out our profile to learn more."
        return text

    display_name = other_user.full_name or other_user.username
    return (
        f"Hi! \U0001F44B You're messaging {display_name}. "
        "Check out my profile to know more about me."
    )


# ==========================================================================
# Conversations
# ==========================================================================

def _find_existing_direct_conversation(
    db: Session, user_id: int, other_id: int
) -> models.Conversation | None:
    """A 1:1 (non-group) conversation already containing exactly these two
    people, if one exists."""
    my_conversation_ids = select(models.ConversationParticipant.conversation_id).where(
        models.ConversationParticipant.user_id == user_id
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
        participant_ids = {p.user_id for p in conv.participants}
        if participant_ids == {user_id, other_id}:
            return conv
    return None


def get_or_create_direct_conversation(
    db: Session, current_user: models.User, other_user: models.User
) -> tuple[models.Conversation, bool]:
    """Reuses an existing 1:1 thread with `other_user`, or starts one
    (message-request gating + the other person's auto-intro DM, exactly
    like POST /conversations below) — factored out so callers that need a
    conversation to send into, without going through that endpoint, get the
    same behavior. Used by create_conversation itself for the 1:1 case, and
    by share_routes.share_internal when sharing a post/reel to someone
    there's no existing thread with yet. Returns (conversation, is_new)."""
    if is_blocked(db, current_user.id, other_user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Can't message this user"
        )

    existing = _find_existing_direct_conversation(db, current_user.id, other_user.id)
    if existing is not None:
        return existing, False

    conversation = models.Conversation(is_group=False, title=None)
    db.add(conversation)
    db.flush()

    # Message requests: for a brand-new 1:1 thread, if the recipient
    # doesn't already follow the sender, the thread lands in the
    # recipient's Message Requests (GET /api/chat/requests) instead of
    # their main inbox until they accept/decline — same as Instagram.
    recipient_follows_sender = (
        db.query(models.Follow)
        .filter(
            models.Follow.follower_id == other_user.id,
            models.Follow.following_id == current_user.id,
        )
        .first()
        is not None
    )
    recipient_status = (
        models.ParticipantStatus.accepted if recipient_follows_sender
        else models.ParticipantStatus.pending
    )
    db.add(models.ConversationParticipant(
        conversation_id=conversation.id, user_id=current_user.id,
        status=models.ParticipantStatus.accepted,
    ))
    db.add(models.ConversationParticipant(
        conversation_id=conversation.id, user_id=other_user.id, status=recipient_status,
    ))

    # Brand-new 1:1 thread — send the other person's auto-intro DM, as if
    # it came from them, before either side has typed anything.
    intro_text = _build_intro_message(other_user)
    db.add(models.Message(
        conversation_id=conversation.id,
        sender_id=other_user.id,
        content=intro_text,
        is_auto_message=True,
    ))

    db.commit()
    db.refresh(conversation)
    return conversation, True


@router.post(
    "/conversations", response_model=schemas.ConversationOut, status_code=status.HTTP_201_CREATED
)
def create_conversation(
    payload: schemas.ConversationCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    other_ids = [uid for uid in payload.participant_ids if uid != current_user.id]
    if not other_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A conversation needs at least one other participant",
        )

    others = db.query(models.User).filter(models.User.id.in_(other_ids)).all()
    found_ids = {u.id for u in others}
    missing = [uid for uid in other_ids if uid not in found_ids]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User(s) not found: {', '.join(str(m) for m in missing)}",
        )

    is_group = len(other_ids) > 1

    if not is_group:
        conversation, is_new = get_or_create_direct_conversation(db, current_user, others[0])
        out = _to_conversation_out(db, conversation, viewer_id=current_user.id)
        out.is_new_conversation = is_new
        out.profile_message = (
            schemas.ProfileMessageOut(
                message=_build_intro_message(others[0]),
                profile_id=others[0].id,
                account_type=others[0].account_type,
            )
            if is_new else None
        )
        return out

    for uid in other_ids:
        if is_blocked(db, current_user.id, uid):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Can't message this user"
            )

    conversation = models.Conversation(is_group=True, title=payload.title)
    db.add(conversation)
    db.flush()

    all_participant_ids = {current_user.id, *other_ids}
    for uid in all_participant_ids:
        db.add(models.ConversationParticipant(
            conversation_id=conversation.id, user_id=uid, status=models.ParticipantStatus.accepted
        ))

    db.commit()
    db.refresh(conversation)

    out = _to_conversation_out(db, conversation, viewer_id=current_user.id)
    out.is_new_conversation = True
    out.profile_message = None
    return out


@router.get("/conversations", response_model=schemas.ConversationsResponse)
def get_conversations(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Only conversations this user has accepted — pending message
    requests live in GET /api/chat/requests until acted on."""
    conversations = (
        db.query(models.Conversation)
        .join(models.ConversationParticipant)
        .filter(
            models.ConversationParticipant.user_id == current_user.id,
            models.ConversationParticipant.status == models.ParticipantStatus.accepted,
        )
        .options(joinedload(models.Conversation.participants).joinedload(
            models.ConversationParticipant.user
        ))
        .order_by(models.Conversation.created_at.desc())
        .all()
    )
    items = [_to_conversation_out(db, c, viewer_id=current_user.id) for c in conversations]
    # Most recently active conversation first.
    items.sort(
        key=lambda c: c.last_message.created_at if c.last_message else c.created_at,
        reverse=True,
    )
    return schemas.ConversationsResponse(items=items)


@router.get("/requests", response_model=schemas.ConversationsResponse)
def get_message_requests(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """1:1 threads started by someone this user doesn't yet follow —
    sitting here, unnotified-as-a-normal-DM, until accepted or declined."""
    conversations = (
        db.query(models.Conversation)
        .join(models.ConversationParticipant)
        .filter(
            models.ConversationParticipant.user_id == current_user.id,
            models.ConversationParticipant.status == models.ParticipantStatus.pending,
        )
        .options(joinedload(models.Conversation.participants).joinedload(
            models.ConversationParticipant.user
        ))
        .order_by(models.Conversation.created_at.desc())
        .all()
    )
    items = [_to_conversation_out(db, c, viewer_id=current_user.id) for c in conversations]
    return schemas.ConversationsResponse(items=items)


@router.post(
    "/conversations/{conversation_id}/accept",
    response_model=schemas.ConversationRequestActionResponse,
)
def accept_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    participant = _get_participant_or_403(db, conversation_id, current_user.id)
    participant.status = models.ParticipantStatus.accepted
    db.commit()
    return schemas.ConversationRequestActionResponse(
        message="Message request accepted", conversation_id=conversation_id
    )


@router.post(
    "/conversations/{conversation_id}/decline",
    response_model=schemas.ConversationRequestActionResponse,
)
def decline_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Declining a request deletes the conversation outright (same as
    Instagram deleting a declined DM request) — only meaningful for the
    1:1, still-pending case, since that's the only kind a participant can
    be 'pending' in."""
    participant = _get_participant_or_403(db, conversation_id, current_user.id)
    if participant.status != models.ParticipantStatus.pending:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="This conversation isn't a pending request"
        )
    conversation = _get_conversation_or_404(db, conversation_id)
    db.delete(conversation)
    db.commit()
    return schemas.ConversationRequestActionResponse(
        message="Message request declined", conversation_id=conversation_id
    )


@router.post("/conversations/{conversation_id}/mute", response_model=schemas.MessageResponse)
def mute_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _get_participant_or_403(db, conversation_id, current_user.id)
    existing = (
        db.query(models.ConversationMute)
        .filter(
            models.ConversationMute.user_id == current_user.id,
            models.ConversationMute.conversation_id == conversation_id,
        )
        .first()
    )
    if existing is None:
        db.add(models.ConversationMute(user_id=current_user.id, conversation_id=conversation_id))
        db.commit()
    return schemas.MessageResponse(message="Conversation muted")


@router.delete("/conversations/{conversation_id}/mute", response_model=schemas.MessageResponse)
def unmute_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = (
        db.query(models.ConversationMute)
        .filter(
            models.ConversationMute.user_id == current_user.id,
            models.ConversationMute.conversation_id == conversation_id,
        )
        .first()
    )
    if row is not None:
        db.delete(row)
        db.commit()
    return schemas.MessageResponse(message="Conversation unmuted")


@router.delete("/conversations/{conversation_id}", response_model=schemas.MessageResponse)
def delete_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Removes the conversation from the caller's own inbox — same as
    WhatsApp/Instagram's "delete chat": the other participant(s) keep
    their copy and their message history untouched. Implemented by
    removing just the caller's ConversationParticipant row; once nobody
    is left in a conversation, it (and its messages) are deleted outright.
    """
    _get_conversation_or_404(db, conversation_id)
    my_participant = _get_participant_or_403(db, conversation_id, current_user.id)

    db.delete(my_participant)
    db.commit()

    remaining = (
        db.query(models.ConversationParticipant)
        .filter(models.ConversationParticipant.conversation_id == conversation_id)
        .count()
    )
    if remaining == 0:
        conversation = _get_conversation_or_404(db, conversation_id)
        db.delete(conversation)  # cascades messages, reactions, statuses
        db.commit()

    return schemas.MessageResponse(message="Conversation deleted")


@router.get("/users/{user_id}/online", response_model=schemas.OnlineStatusOut)
def get_online_status(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Whether a user currently has an open chat WebSocket connection."""
    exists = db.query(models.User.id).filter(models.User.id == user_id).first()
    if exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return schemas.OnlineStatusOut(user_id=user_id, is_online=manager.is_online(user_id))


# ==========================================================================
# Messages
# ==========================================================================

@router.get(
    "/conversations/{conversation_id}/messages", response_model=schemas.PaginatedMessagesResponse
)
def get_messages(
    conversation_id: int,
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _get_conversation_or_404(db, conversation_id)
    _require_participant(db, conversation_id, current_user.id)

    query = db.query(models.Message).filter(models.Message.conversation_id == conversation_id)
    total = query.count()
    # Most recent first, same convention as every other paginated feed here.
    messages = (
        query.order_by(models.Message.created_at.desc()).offset(offset).limit(limit).all()
    )

    # Fetching your messages counts as your device having received them —
    # covers the case where you were offline when they were sent (send_message
    # only marks delivered_at immediately for recipients who were online).
    message_ids = [m.id for m in messages if m.sender_id != current_user.id]
    if message_ids:
        undelivered = (
            db.query(models.MessageStatus)
            .filter(
                models.MessageStatus.message_id.in_(message_ids),
                models.MessageStatus.user_id == current_user.id,
                models.MessageStatus.delivered_at.is_(None),
            )
            .all()
        )
        if undelivered:
            now = datetime.now(timezone.utc)
            for s in undelivered:
                s.delivered_at = now
            db.commit()

    items = [_to_message_out(db, m, current_user.id) for m in messages]
    return schemas.PaginatedMessagesResponse(total=total, limit=limit, offset=offset, items=items)


@router.get(
    "/conversations/{conversation_id}/media", response_model=schemas.PaginatedMessagesResponse
)
def get_conversation_media(
    conversation_id: int,
    media_type: models.MediaType | None = Query(
        default=None, description="Filter to just image, video, or audio (voice notes)"
    ),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The conversation's media gallery — every image/video/voice-note
    message, newest first, optionally narrowed to one media_type. Same
    shape as GET .../messages (still individual messages, just filtered to
    ones with an attachment) so a client can reuse its message-rendering
    code for grid/gallery view."""
    _get_conversation_or_404(db, conversation_id)
    _require_participant(db, conversation_id, current_user.id)

    query = db.query(models.Message).filter(
        models.Message.conversation_id == conversation_id,
        models.Message.media_url.isnot(None),
    )
    if media_type is not None:
        query = query.filter(models.Message.media_type == media_type)

    total = query.count()
    messages = query.order_by(models.Message.created_at.desc()).offset(offset).limit(limit).all()
    items = [_to_message_out(db, m, current_user.id) for m in messages]
    return schemas.PaginatedMessagesResponse(total=total, limit=limit, offset=offset, items=items)


async def _push_message_event(
    db: Session,
    recipient_ids: list[int],
    message: models.Message,
    event_type: str,
    default_out: schemas.MessageOut,
) -> None:
    """Send a "message" / "message_edited" socket event. Normally every
    recipient gets the same payload; a shared reel/post's preview depends on
    who's looking, so those are built and sent per recipient."""
    base = {"type": event_type, "conversation_id": message.conversation_id}
    if message.shared_reel_id is None and message.shared_post_id is None:
        await manager.send_to_users(
            recipient_ids, {**base, "message": default_out.model_dump(mode="json")}
        )
        return
    for uid in recipient_ids:
        out = _to_message_out(db, message, viewer_id=uid)
        await manager.send_to_users([uid], {**base, "message": out.model_dump(mode="json")})


async def _create_and_dispatch_message(
    db: Session,
    conversation_id: int,
    message: models.Message,
    current_user: models.User,
    preview: str,
) -> schemas.MessageOut:
    """Shared by send_message (text) and send_media_message (image/video/
    voice): persist the message, mark it read for the sender, create
    per-recipient delivery-status rows, push it live over the chat socket,
    and notify anyone who isn't currently connected. `preview` is the
    short text used in that notification (e.g. the caption, or "Sent a
    photo" for a caption-less media message)."""
    db.add(message)
    db.commit()
    db.refresh(message)

    my_participant = _get_participant_or_403(db, conversation_id, current_user.id)
    my_participant.last_read_message_id = message.id
    db.commit()

    participant_ids = _conversation_participant_ids(db, conversation_id)
    recipient_ids = [uid for uid in participant_ids if uid != current_user.id]

    now = datetime.now(timezone.utc)
    for uid in recipient_ids:
        db.add(models.MessageStatus(
            message_id=message.id,
            user_id=uid,
            delivered_at=now if manager.is_online(uid) else None,
        ))
    db.commit()

    message_out = _to_message_out(db, message, current_user.id)

    await _push_message_event(db, recipient_ids, message, "message", message_out)

    offline_ids = [uid for uid in recipient_ids if not manager.is_online(uid)]
    # Muted-conversation participants still get delivery/WS updates above
    # (the thread itself is unaffected) — muting only suppresses the
    # Notification row + push below, same as Instagram's "mute
    # notifications for this chat".
    notify_ids = [uid for uid in offline_ids if not is_conversation_muted(db, uid, conversation_id)]
    for uid in notify_ids:
        await notify_user(
            db,
            user_id=uid,
            actor=current_user,
            notif_type=models.NotificationType.message,
            message=f"{current_user.username} sent you a message",
            target_type="conversation",
            target_id=conversation_id,
            push_body=preview,
        )

    return message_out


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=schemas.MessageOut,
    status_code=status.HTTP_201_CREATED,
)
async def send_message(
    conversation_id: int,
    payload: schemas.MessageCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _get_conversation_or_404(db, conversation_id)
    _get_participant_or_403(db, conversation_id, current_user.id)
    _reject_if_blocked_in_conversation(db, conversation_id, current_user.id)

    reply_to_id = _validate_reply_target(db, conversation_id, payload.reply_to_message_id)

    if payload.shared_reel_id is not None:
        # Share a reel as a card (optionally with a note in `content`).
        _get_shareable_reel_or_404(db, payload.shared_reel_id, current_user.id)
    elif payload.shared_post_id is not None:
        # Share a post as a card (optionally with a note in `content`).
        _get_shareable_post_or_404(db, payload.shared_post_id, current_user.id)

    message = models.Message(
        conversation_id=conversation_id,
        sender_id=current_user.id,
        content=payload.content,
        reply_to_message_id=reply_to_id,
        shared_reel_id=payload.shared_reel_id,
        shared_post_id=payload.shared_post_id,
    )
    default_preview = (
        "Sent a reel" if payload.shared_reel_id is not None
        else "Sent a post" if payload.shared_post_id is not None
        else ""
    )
    text = payload.content or default_preview
    preview = text if len(text) <= 80 else text[:77] + "..."
    return await _create_and_dispatch_message(db, conversation_id, message, current_user, preview)


@router.post(
    "/conversations/{conversation_id}/media",
    response_model=schemas.MediaMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def send_media_message(
    conversation_id: int,
    file: UploadFile,
    caption: str | None = Form(default=None),
    reply_to_message_id: int | None = Form(default=None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Send an image, video, or voice note. `file`'s content-type decides
    which: images and videos go through the same validation as posts/reels
    (see save_upload_file); anything under `audio/*` is treated as a voice
    note. `caption` is optional text alongside the media."""
    _get_conversation_or_404(db, conversation_id)
    _get_participant_or_403(db, conversation_id, current_user.id)
    _reject_if_blocked_in_conversation(db, conversation_id, current_user.id)

    reply_to_id = _validate_reply_target(db, conversation_id, reply_to_message_id)

    content_type = (file.content_type or "").lower()
    if content_type.startswith("audio/"):
        url, kind = save_upload_file(file, "chat_voice", allow_audio=True)
    else:
        url, kind = save_upload_file(file, "chat_media", allow_video=True)

    media_type = {
        "image": models.MediaType.image,
        "video": models.MediaType.video,
        "audio": models.MediaType.audio,
    }[kind]

    message = models.Message(
        conversation_id=conversation_id,
        sender_id=current_user.id,
        content=caption,
        media_url=url,
        media_type=media_type,
        reply_to_message_id=reply_to_id,
    )
    preview = caption or {"image": "Sent a photo", "video": "Sent a video", "audio": "Sent a voice message"}[kind]
    message_out = await _create_and_dispatch_message(db, conversation_id, message, current_user, preview)
    return schemas.MediaMessageResponse(
        id=message_out.id,
        conversation_id=message_out.conversation_id,
        sender_id=message_out.sender_id,
        content=message_out.content,
        media_url=message_out.media_url,
        media_type=message_out.media_type,
        reply_to_message_id=message_out.reply_to_message_id,
        created_at=message_out.created_at,
    )


@router.post("/conversations/{conversation_id}/read", response_model=schemas.MarkReadResponse)
async def mark_conversation_read(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Marks everything currently in the conversation as read by the caller,
    and lets other participants know via a 'read' WebSocket event."""
    _get_conversation_or_404(db, conversation_id)
    participant = _get_participant_or_403(db, conversation_id, current_user.id)

    latest = (
        db.query(models.Message)
        .filter(models.Message.conversation_id == conversation_id)
        .order_by(models.Message.id.desc())
        .first()
    )
    if latest is not None:
        participant.last_read_message_id = latest.id
        db.commit()

        now = datetime.now(timezone.utc)
        statuses = (
            db.query(models.MessageStatus)
            .join(models.Message, models.Message.id == models.MessageStatus.message_id)
            .filter(
                models.Message.conversation_id == conversation_id,
                models.MessageStatus.user_id == current_user.id,
                models.MessageStatus.read_at.is_(None),
            )
            .all()
        )
        for s in statuses:
            s.read_at = now
            if s.delivered_at is None:
                s.delivered_at = now  # reading implies it was delivered
        if statuses:
            db.commit()

    other_ids = [uid for uid in _conversation_participant_ids(db, conversation_id) if uid != current_user.id]
    await manager.send_to_users(
        other_ids,
        {
            "type": "read",
            "conversation_id": conversation_id,
            "user_id": current_user.id,
            "last_read_message_id": latest.id if latest else None,
        },
    )

    return schemas.MarkReadResponse(
        message="Marked as read", last_read_message_id=latest.id if latest else None
    )


def _get_message_or_404(db: Session, message_id: int) -> models.Message:
    message = db.query(models.Message).filter(models.Message.id == message_id).first()
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    return message


@router.put("/messages/{message_id}", response_model=schemas.MessageOut)
async def edit_message(
    message_id: int,
    payload: schemas.MessageEditRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    message = _get_message_or_404(db, message_id)
    if message.sender_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only edit your own messages"
        )
    if message.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Can't edit a deleted message"
        )

    message.content = payload.content
    message.edited_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(message)

    message_out = _to_message_out(db, message, current_user.id)
    other_ids = [
        uid for uid in _conversation_participant_ids(db, message.conversation_id)
        if uid != current_user.id
    ]
    await _push_message_event(db, other_ids, message, "message_edited", message_out)
    return message_out


@router.delete("/messages/{message_id}", response_model=schemas.MessageResponse)
async def delete_message(
    message_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Soft delete — the row and its history stay (for moderation/audit),
    but content is never returned again once deleted; see _to_message_out.
    This deletes for everyone in the conversation, not just the caller."""
    message = _get_message_or_404(db, message_id)
    if message.sender_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only delete your own messages"
        )

    message.is_deleted = True
    message.deleted_at = datetime.now(timezone.utc)
    db.query(models.MessageReaction).filter(
        models.MessageReaction.message_id == message_id
    ).delete(synchronize_session=False)
    db.commit()

    other_ids = [
        uid for uid in _conversation_participant_ids(db, message.conversation_id)
        if uid != current_user.id
    ]
    await manager.send_to_users(
        other_ids,
        {
            "type": "message_deleted",
            "conversation_id": message.conversation_id,
            "message_id": message_id,
        },
    )
    return schemas.MessageResponse(message="Message deleted")


@router.post("/messages/{message_id}/react", response_model=schemas.MessageOut)
async def react_to_message(
    message_id: int,
    payload: schemas.MessageReactionCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    message = _get_message_or_404(db, message_id)
    _require_participant(db, message.conversation_id, current_user.id)
    if message.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Can't react to a deleted message"
        )

    existing = (
        db.query(models.MessageReaction)
        .filter(
            models.MessageReaction.message_id == message_id,
            models.MessageReaction.user_id == current_user.id,
        )
        .first()
    )
    if existing is not None:
        existing.emoji = payload.emoji
    else:
        db.add(models.MessageReaction(
            message_id=message_id, user_id=current_user.id, emoji=payload.emoji
        ))
    db.commit()
    db.refresh(message)

    message_out = _to_message_out(db, message, current_user.id)
    other_ids = [
        uid for uid in _conversation_participant_ids(db, message.conversation_id)
        if uid != current_user.id
    ]
    await manager.send_to_users(
        other_ids,
        {
            "type": "message_reaction",
            "conversation_id": message.conversation_id,
            "message_id": message_id,
            "user_id": current_user.id,
            "emoji": payload.emoji,
        },
    )
    return message_out


@router.delete("/messages/{message_id}/react", response_model=schemas.MessageOut)
async def remove_message_reaction(
    message_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    message = _get_message_or_404(db, message_id)
    _require_participant(db, message.conversation_id, current_user.id)

    existing = (
        db.query(models.MessageReaction)
        .filter(
            models.MessageReaction.message_id == message_id,
            models.MessageReaction.user_id == current_user.id,
        )
        .first()
    )
    if existing is not None:
        db.delete(existing)
        db.commit()
        db.refresh(message)

        other_ids = [
            uid for uid in _conversation_participant_ids(db, message.conversation_id)
            if uid != current_user.id
        ]
        await manager.send_to_users(
            other_ids,
            {
                "type": "message_reaction_removed",
                "conversation_id": message.conversation_id,
                "message_id": message_id,
                "user_id": current_user.id,
            },
        )

    return _to_message_out(db, message, current_user.id)


# ==========================================================================
# Chat settings
# ==========================================================================

@router.put("/settings/font", response_model=schemas.ChatFontResponse)
def update_chat_font(
    payload: schemas.ChatFontUpdateRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    current_user.chat_font = payload.font
    db.commit()
    return schemas.ChatFontResponse(message="Chat font updated", font=payload.font)


# ==========================================================================
# WebSocket — real-time delivery, typing indicators, presence
# ==========================================================================
#
# Connect with:  ws(s)://<host>/api/chat/ws?token=<access_token>
#
# The browser WebSocket API can't set an Authorization header, so the access
# token travels as a query parameter here instead (same token you already
# use for REST calls).
#
# Client -> server messages (JSON):
#   {"type": "typing", "conversation_id": 1}
#   {"type": "ping"}
#
# Server -> client messages (JSON):
#   {"type": "message", "conversation_id": 1, "message": {...MessageOut}}
#   {"type": "typing", "conversation_id": 1, "user_id": 4}
#   {"type": "read", "conversation_id": 1, "user_id": 4, "last_read_message_id": 12}
#   {"type": "presence", "user_id": 4, "status": "online" | "offline"}
#   {"type": "pong"}
#   {"type": "error", "detail": "..."}

def _user_conversation_partner_ids(db: Session, user_id: int) -> set[int]:
    """Every other user who shares a conversation with this user — the
    audience for that user's presence changes."""
    conv_ids = select(models.ConversationParticipant.conversation_id).where(
        models.ConversationParticipant.user_id == user_id
    )
    rows = (
        db.query(models.ConversationParticipant.user_id)
        .filter(
            models.ConversationParticipant.conversation_id.in_(conv_ids),
            models.ConversationParticipant.user_id != user_id,
        )
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


@router.websocket("/ws")
async def chat_websocket(websocket: WebSocket, token: str = Query(...)):
    db = SessionLocal()
    try:
        user = get_user_from_raw_token(token, db)
        if user is None:
            await websocket.close(code=4401)
            return

        user_id = user.id
        partner_ids = _user_conversation_partner_ids(db, user_id)
        just_came_online = await manager.connect(user_id, websocket)

        if just_came_online and partner_ids:
            await manager.send_to_users(
                partner_ids, {"type": "presence", "user_id": user_id, "status": "online"}
            )

        try:
            while True:
                data = await websocket.receive_json()
                event_type = data.get("type")

                if event_type == "ping":
                    await websocket.send_json({"type": "pong"})

                elif event_type == "typing":
                    conversation_id = data.get("conversation_id")
                    if not isinstance(conversation_id, int):
                        await websocket.send_json({"type": "error", "detail": "conversation_id is required"})
                        continue
                    is_participant = (
                        db.query(models.ConversationParticipant.id)
                        .filter(
                            models.ConversationParticipant.conversation_id == conversation_id,
                            models.ConversationParticipant.user_id == user_id,
                        )
                        .first()
                        is not None
                    )
                    if not is_participant:
                        await websocket.send_json({"type": "error", "detail": "Not a participant in that conversation"})
                        continue
                    others = [
                        uid for uid in _conversation_participant_ids(db, conversation_id) if uid != user_id
                    ]
                    await manager.send_to_users(
                        others,
                        {"type": "typing", "conversation_id": conversation_id, "user_id": user_id},
                    )

                else:
                    await websocket.send_json({"type": "error", "detail": f"Unknown event type '{event_type}'"})

        except WebSocketDisconnect:
            pass
        finally:
            just_went_offline = await manager.disconnect(user_id, websocket)
            if just_went_offline and partner_ids:
                await manager.send_to_users(
                    partner_ids, {"type": "presence", "user_id": user_id, "status": "offline"}
                )
    finally:
        db.close()
