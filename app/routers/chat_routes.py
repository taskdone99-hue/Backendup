import logging

from fastapi import (
    APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
)
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.database import get_db, SessionLocal
from app import models, schemas
from app.auth import get_current_user, get_user_from_raw_token
from app.ws_manager import manager
from app.services.push_service import send_push

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
        last_message=schemas.MessageOut.model_validate(last_message) if last_message else None,
        unread_count=unread_count,
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
    # people instead of creating a duplicate thread every time.
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
                return _to_conversation_out(db, conv, viewer_id=current_user.id)

    conversation = models.Conversation(is_group=is_group, title=payload.title if is_group else None)
    db.add(conversation)
    db.flush()

    all_participant_ids = {current_user.id, *other_ids}
    for uid in all_participant_ids:
        db.add(models.ConversationParticipant(conversation_id=conversation.id, user_id=uid))

    db.commit()
    db.refresh(conversation)
    return _to_conversation_out(db, conversation, viewer_id=current_user.id)


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
    items = (
        query.order_by(models.Message.created_at.desc()).offset(offset).limit(limit).all()
    )
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

    message_out = schemas.MessageOut.model_validate(message)
    participant_ids = _conversation_participant_ids(db, conversation_id)
    recipient_ids = [uid for uid in participant_ids if uid != current_user.id]

    # Live delivery to anyone with the chat open right now.
    await manager.send_to_users(
        recipient_ids,
        {"type": "message", "conversation_id": conversation_id, "message": message_out.model_dump(mode="json")},
    )

    # Push notification for recipients who are NOT currently connected —
    # they won't see the WebSocket event above, so this is how they find
    # out. No-ops cleanly if FCM isn't configured (see push_service).
    offline_ids = [uid for uid in recipient_ids if not manager.is_online(uid)]
    if offline_ids:
        tokens = (
            db.query(models.DeviceToken.token)
            .filter(models.DeviceToken.user_id.in_(offline_ids))
            .all()
        )
        token_list = [t[0] for t in tokens]
        if token_list:
            preview = payload.content if len(payload.content) <= 80 else payload.content[:77] + "..."
            send_push(
                token_list,
                title=current_user.username,
                body=preview,
                data={"type": "message", "conversation_id": str(conversation_id)},
            )
        for uid in offline_ids:
            db.add(models.Notification(
                user_id=uid,
                actor_id=current_user.id,
                type=models.NotificationType.message,
                message=f"{current_user.username} sent you a message",
                target_type="conversation",
                target_id=conversation_id,
            ))
        db.commit()

    return message


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
