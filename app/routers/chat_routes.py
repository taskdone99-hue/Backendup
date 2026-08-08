from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user

router = APIRouter(prefix="/api/chat", tags=["chat"])


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


def _require_participant(db: Session, conversation_id: int, user_id: int) -> None:
    is_participant = (
        db.query(models.ConversationParticipant)
        .filter(
            models.ConversationParticipant.conversation_id == conversation_id,
            models.ConversationParticipant.user_id == user_id,
        )
        .first()
        is not None
    )
    if not is_participant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You're not a participant in this conversation",
        )


def _to_conversation_out(
    db: Session, conversation: models.Conversation
) -> schemas.ConversationOut:
    participants = [
        schemas.UserSummaryOut.model_validate(p.user) for p in conversation.participants
    ]
    last_message = (
        db.query(models.Message)
        .filter(models.Message.conversation_id == conversation.id)
        .order_by(models.Message.created_at.desc())
        .first()
    )
    return schemas.ConversationOut(
        id=conversation.id,
        is_group=conversation.is_group,
        title=conversation.title,
        created_at=conversation.created_at,
        participants=participants,
        last_message=schemas.MessageOut.model_validate(last_message) if last_message else None,
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
                return _to_conversation_out(db, conv)

    conversation = models.Conversation(is_group=is_group, title=payload.title if is_group else None)
    db.add(conversation)
    db.flush()

    all_participant_ids = {current_user.id, *other_ids}
    for uid in all_participant_ids:
        db.add(models.ConversationParticipant(conversation_id=conversation.id, user_id=uid))

    db.commit()
    db.refresh(conversation)
    return _to_conversation_out(db, conversation)


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
    items = [_to_conversation_out(db, c) for c in conversations]
    # Most recently active conversation first.
    items.sort(
        key=lambda c: c.last_message.created_at if c.last_message else c.created_at,
        reverse=True,
    )
    return schemas.ConversationsResponse(items=items)


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
def send_message(
    conversation_id: int,
    payload: schemas.MessageCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _get_conversation_or_404(db, conversation_id)
    _require_participant(db, conversation_id, current_user.id)

    message = models.Message(
        conversation_id=conversation_id,
        sender_id=current_user.id,
        content=payload.content,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


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
