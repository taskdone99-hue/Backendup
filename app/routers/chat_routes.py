import logging
from datetime import datetime, timezone

from fastapi import (
    APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
)
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.database import get_db, SessionLocal
from app import models, schemas
from app.auth import get_current_user, get_user_from_raw_token
from app.ws_manager import manager
from app.services.notification_service import notify_user

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


def _to_message_out(db: Session, message: models.Message) -> schemas.MessageOut:
    out = schemas.MessageOut.model_validate(message)
    if message.is_deleted:
        out.content = "This message was deleted"

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
    if viewer_id is not None:
        my_participant = next((p for p in conversation.participants if p.user_id == viewer_id), None)
        if my_participant is not None:
            q = db.query(models.Message).filter(models.Message.conversation_id == conversation.id)
            if my_participant.last_read_message_id is not None:
                q = q.filter(models.Message.id > my_participant.last_read_message_id)
            unread_count = q.filter(models.Message.sender_id != viewer_id).count()

    return schemas.ConversationOut(
        id=conversation.id,
        is_group=conversation.is_group,
        title=conversation.title,
        created_at=conversation.created_at,
        participants=participants,
        last_message=_to_message_out(db, last_message) if last_message else None,
        unread_count=unread_count,
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

    # For a 1:1 chat, reuse an existing conversation between the same two
    # people instead of creating a duplicate thread every time. Finding one
    # here IS the "have they ever messaged before?" check — if a thread
    # already exists (even an empty one), the intro DM has already had its
    # chance to go out, so it never fires twice.
    if not is_group:
        other_id = other_ids[0]
        my_conversation_ids = select(models.ConversationParticipant.conversation_id).where(
            models.ConversationParticipant.user_id == current_user.id
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
            if participant_ids == {current_user.id, other_id}:
                out = _to_conversation_out(db, conv, viewer_id=current_user.id)
                out.is_new_conversation = False
                out.profile_message = None
                return out

    conversation = models.Conversation(is_group=is_group, title=payload.title if is_group else None)
    db.add(conversation)
    db.flush()

    all_participant_ids = {current_user.id, *other_ids}
    for uid in all_participant_ids:
        db.add(models.ConversationParticipant(conversation_id=conversation.id, user_id=uid))

    profile_message_out = None
    if not is_group:
        # Brand-new 1:1 thread — send the other person's auto-intro DM,
        # as if it came from them, before either side has typed anything.
        other_user = others[0]
        intro_text = _build_intro_message(other_user)
        intro = models.Message(
            conversation_id=conversation.id,
            sender_id=other_user.id,
            content=intro_text,
            is_auto_message=True,
        )
        db.add(intro)
        profile_message_out = schemas.ProfileMessageOut(
            message=intro_text,
            profile_id=other_user.id,
            account_type=other_user.account_type,
        )

    db.commit()
    db.refresh(conversation)

    out = _to_conversation_out(db, conversation, viewer_id=current_user.id)
    out.is_new_conversation = True
    out.profile_message = profile_message_out
    return out


@router.get("/conversations", response_model=schemas.ConversationsResponse)
def get_conversations(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    conversations = (
        db.query(models.Conversation)
        .join(models.ConversationParticipant)
        .filter(models.ConversationParticipant.user_id == current_user.id)
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

    items = [_to_message_out(db, m) for m in messages]
    return schemas.PaginatedMessagesResponse(total=total, limit=limit, offset=offset, items=items)


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
    my_participant = _get_participant_or_403(db, conversation_id, current_user.id)

    message = models.Message(
        conversation_id=conversation_id,
        sender_id=current_user.id,
        content=payload.content,
    )
    db.add(message)
    db.commit()
    db.refresh(message)

    # Sending your own message counts as having read up to it — otherwise
    # you'd immediately see your own message as "unread".
    my_participant.last_read_message_id = message.id
    db.commit()

    participant_ids = _conversation_participant_ids(db, conversation_id)
    recipient_ids = [uid for uid in participant_ids if uid != current_user.id]

    # One MessageStatus row per recipient — delivered immediately if
    # they're online right now (the WS push below reaches them live),
    # otherwise left null until they next fetch this conversation.
    now = datetime.now(timezone.utc)
    for uid in recipient_ids:
        db.add(models.MessageStatus(
            message_id=message.id,
            user_id=uid,
            delivered_at=now if manager.is_online(uid) else None,
        ))
    db.commit()

    message_out = _to_message_out(db, message)

    # Live delivery to anyone with the chat open right now.
    await manager.send_to_users(
        recipient_ids,
        {"type": "message", "conversation_id": conversation_id, "message": message_out.model_dump(mode="json")},
    )

    # Notification for recipients who are NOT currently connected to the
    # chat socket — they won't see the "message" WS event above, so this is
    # how they find out. Routed through notify_user() (the same fan-out
    # used by follow/follow-request/story notifications) so it: (1) writes
    # the Notification row, (2) pushes a live
    # {"type":"notification","notification":{...}} event to their
    # notifications-socket connection if they have one open, and (3) sends
    # an FCM push to their devices. Previously this block only did (1) and
    # (3) directly, which is why a recipient connected to
    # /api/notifications/ws but not /api/chat/ws never got a live push for
    # a new message — notify_user() closes that gap.
    offline_ids = [uid for uid in recipient_ids if not manager.is_online(uid)]
    if offline_ids:
        preview = payload.content if len(payload.content) <= 80 else payload.content[:77] + "..."
        for uid in offline_ids:
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

    message_out = _to_message_out(db, message)
    other_ids = [
        uid for uid in _conversation_participant_ids(db, message.conversation_id)
        if uid != current_user.id
    ]
    await manager.send_to_users(
        other_ids,
        {
            "type": "message_edited",
            "conversation_id": message.conversation_id,
            "message": message_out.model_dump(mode="json"),
        },
    )
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

    message_out = _to_message_out(db, message)
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

    return _to_message_out(db, message)


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
