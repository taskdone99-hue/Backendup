"""
Interactive chat options — WhatsApp-Business-style quick-reply buttons that a
Business or Premium account attaches to one of its chat messages.

Options ride on the existing chat system (no second message system): every
message response already carries `options` (see chat_routes._to_message_out),
so the client renders buttons straight from the normal message list / socket
event. The endpoints here create, edit and remove them, record taps, and
report how often each is used.

Authentication: every endpoint needs the usual `Authorization: Bearer <token>`.
Eligibility is decided server-side (Business account, or active Premium
membership) — see services/chat_option_service.py.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, exists, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import get_current_user
from app.database import get_db
from app.routers.chat_routes import (
    _conversation_participant_ids,
    _get_conversation_or_404,
    _get_message_or_404,
    _get_participant_or_403,
    _reject_if_blocked_in_conversation,
    _to_message_out,
)
from app.services import chat_option_service as svc
from app.ws_manager import manager

router = APIRouter(prefix="/api/chat", tags=["chat-options"])

_NOT_ELIGIBLE = {403: {"description": "Not a Business/Premium account, or not the owner of the message"}}


# ---- helpers ----

def _get_option_or_404(db: Session, option_id: int) -> models.ChatMessageOption:
    option = (
        db.query(models.ChatMessageOption)
        .filter(models.ChatMessageOption.id == option_id)
        .first()
    )
    if option is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Option not found")
    return option


def _require_message_owner(message: models.Message, user: models.User) -> None:
    if message.sender_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only manage options on your own messages",
        )


def _require_editable(db: Session, message: models.Message, user: models.User) -> None:
    """Shared by create/update: owner, still in the conversation, message alive."""
    _require_message_owner(message, user)
    _get_participant_or_403(db, message.conversation_id, user.id)
    if message.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Can't add options to a deleted message"
        )


def _options_out(message: models.Message, viewer_id: int) -> schemas.ChatMessageOptionsOut:
    return schemas.ChatMessageOptionsOut(
        message_id=message.id,
        text=message.content,
        options=[
            schemas.ChatOptionOut.model_validate(o) for o in svc.visible_options(message, viewer_id)
        ],
    )


async def _push_options_changed(db: Session, message: models.Message, actor: models.User) -> None:
    """Tell the other participants (live) that this message's buttons changed —
    same "message_edited" socket event the client already handles. Built per
    recipient so a disabled option is never leaked to them."""
    for uid in _conversation_participant_ids(db, message.conversation_id):
        if uid == actor.id:
            continue
        out = _to_message_out(db, message, viewer_id=uid)
        await manager.send_to_users(
            [uid],
            {
                "type": "message_edited",
                "conversation_id": message.conversation_id,
                "message": out.model_dump(mode="json"),
            },
        )


# ---- create ----

@router.post(
    "/options",
    response_model=schemas.ChatMessageOptionsOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add clickable options to one of your messages",
    responses={
        **_NOT_ELIGIBLE,
        400: {"description": "Invalid title/action, empty list, deleted message, or more than 5 options in total"},
        404: {"description": "Message not found"},
        409: {"description": "Duplicate title or action on this message"},
    },
)
async def create_options(
    payload: schemas.ChatOptionsCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Attaches 1-5 buttons to a message **you sent**.

    **Who can call it:** Business accounts (`account_type = business`) and
    users with an active Premium membership. Anyone else gets **403**; so does
    a user who doesn't own the message. Eligibility is checked on the server.

    **Rules**
    - Max **5** options per message in total (existing + new) — 400 beyond that.
    - `title`: 1-24 characters, no line breaks. `action`: 1-50 characters of
      letters, digits, `_ - . :` (e.g. `track_order`).
    - Titles must be unique within the message, and so must actions
      (case-insensitive) — 409 if one already exists.

    Options are shown in the order sent, after any existing ones. Other
    participants receive a live `message_edited` socket event.
    """
    svc.require_eligible_sender(db, current_user)
    message = _get_message_or_404(db, payload.message_id)
    _require_editable(db, message, current_user)

    existing = list(message.options)
    total = len(existing) + len(payload.options)
    if total > svc.MAX_OPTIONS_PER_MESSAGE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"A message can have at most {svc.MAX_OPTIONS_PER_MESSAGE} options "
            f"(it already has {len(existing)})",
        )
    for new in payload.options:
        clash = svc.find_duplicate(existing, new.title, new.action)
        if clash:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=clash)

    next_order = max((o.display_order for o in existing), default=-1) + 1
    for offset, new in enumerate(payload.options):
        message.options.append(models.ChatMessageOption(
            title=new.title, action=new.action, display_order=next_order + offset
        ))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Duplicate option action on this message"
        )
    db.refresh(message)
    await _push_options_changed(db, message, current_user)
    return _options_out(message, current_user.id)


# ---- read ----

@router.get(
    "/options/{conversation_id}",
    response_model=schemas.PaginatedChatOptionsResponse,
    summary="Interactive messages (text + options) in a conversation",
    responses={
        403: {"description": "You're not a participant in this conversation"},
        404: {"description": "Conversation not found"},
    },
)
def get_conversation_options(
    conversation_id: int,
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Newest first. Lists only messages that have options. Participants see only
    **enabled** options; the sender also sees the ones they've disabled.

    You normally don't need this: `GET /api/chat/conversations/{id}/messages`
    already returns `options` on every message. Use this to build a menu/
    shortcut list.
    """
    _get_conversation_or_404(db, conversation_id)
    _get_participant_or_403(db, conversation_id, current_user.id)

    any_option = exists().where(models.ChatMessageOption.message_id == models.Message.id)
    enabled_option = exists().where(and_(
        models.ChatMessageOption.message_id == models.Message.id,
        models.ChatMessageOption.is_enabled.is_(True),
    ))
    query = db.query(models.Message).filter(
        models.Message.conversation_id == conversation_id,
        models.Message.is_deleted.is_(False),
        or_(and_(models.Message.sender_id == current_user.id, any_option), enabled_option),
    )
    total = query.count()
    messages = query.order_by(models.Message.created_at.desc(), models.Message.id.desc()) \
        .offset(offset).limit(limit).all()
    return schemas.PaginatedChatOptionsResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[_options_out(m, current_user.id) for m in messages],
    )


# ---- update / delete ----

@router.put(
    "/options/{option_id}",
    response_model=schemas.ChatOptionOut,
    summary="Edit one of your options",
    responses={
        **_NOT_ELIGIBLE,
        400: {"description": "Invalid title/action, nothing to update, or deleted message"},
        404: {"description": "Option not found"},
        409: {"description": "Duplicate title or action on this message"},
    },
)
async def update_option(
    option_id: int,
    payload: schemas.ChatOptionUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Change `title`, `action` and/or `is_enabled` (send only what changes).
    Setting `is_enabled: false` hides the button from recipients and makes it
    unselectable without deleting its analytics.

    Only the **sender of the original message** can edit, and they must still
    be a Business / Premium account (403 otherwise). Same title/action rules as
    creation; renaming onto an existing title/action of the same message is 409.
    """
    option = _get_option_or_404(db, option_id)
    message = option.message
    _require_message_owner(message, current_user)
    svc.require_eligible_sender(db, current_user)
    _require_editable(db, message, current_user)

    new_title = payload.title if payload.title is not None else option.title
    new_action = payload.action if payload.action is not None else option.action
    clash = svc.find_duplicate(list(message.options), new_title, new_action, exclude_id=option.id)
    if clash:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=clash)

    option.title = new_title
    option.action = new_action
    if payload.is_enabled is not None:
        option.is_enabled = payload.is_enabled
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Duplicate option action on this message"
        )
    db.refresh(option)
    db.refresh(message)
    await _push_options_changed(db, message, current_user)
    return schemas.ChatOptionOut.model_validate(option)


@router.delete(
    "/options/{option_id}",
    response_model=schemas.MessageResponse,
    summary="Delete one of your options",
    responses={
        403: {"description": "Not the sender of the original message"},
        404: {"description": "Option not found"},
    },
)
async def delete_option(
    option_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Permanently removes the option and its recorded selections. Only the
    sender of the original message can do this (403 otherwise) — it stays
    available to them even if their Business/Premium status has lapsed, so
    they can clean up.
    """
    option = _get_option_or_404(db, option_id)
    message = option.message
    _require_message_owner(message, current_user)
    db.delete(option)
    db.commit()
    db.refresh(message)
    await _push_options_changed(db, message, current_user)
    return schemas.MessageResponse(message="Option deleted")


# ---- select ----

@router.post(
    "/messages/{message_id}/option",
    response_model=schemas.ChatOptionSelectionOut,
    summary="Tap an option on a message",
    responses={
        400: {"description": "Option disabled, or you're the sender of the message"},
        403: {"description": "You're not a participant in this conversation (or blocked)"},
        404: {"description": "Message not found, or the option isn't on this message"},
    },
)
def select_option(
    message_id: int,
    payload: schemas.ChatOptionSelectRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Records that you chose `option_id` on this message. Any participant of the
    conversation except the message's own sender can do this.

    - The option must belong to **this** message (404 otherwise) and be enabled
      (400 otherwise).
    - Idempotent: tapping the same option again returns the same response and
      is not counted twice (analytics count distinct users).
    - Different options on the same message may each be selected.
    """
    message = _get_message_or_404(db, message_id)
    _get_participant_or_403(db, message.conversation_id, current_user.id)
    _reject_if_blocked_in_conversation(db, message.conversation_id, current_user.id)
    if message.is_deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    if message.sender_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You can't select options on your own message",
        )

    option = (
        db.query(models.ChatMessageOption)
        .filter(
            models.ChatMessageOption.id == payload.option_id,
            models.ChatMessageOption.message_id == message_id,
        )
        .first()
    )
    if option is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Option not found on this message"
        )
    if not option.is_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="This option is no longer available"
        )

    already = (
        db.query(models.ChatMessageOptionSelection.id)
        .filter(
            models.ChatMessageOptionSelection.option_id == option.id,
            models.ChatMessageOptionSelection.user_id == current_user.id,
        )
        .first()
    )
    if already is None:
        db.add(models.ChatMessageOptionSelection(option_id=option.id, user_id=current_user.id))
        try:
            db.commit()
        except IntegrityError:  # concurrent double-tap: the other request recorded it
            db.rollback()

    return schemas.ChatOptionSelectionOut(
        message_id=message_id,
        option_id=option.id,
        title=option.title,
        action=option.action,
        selected=True,
    )


# ---- analytics ----

@router.get(
    "/options/{option_id}/analytics",
    response_model=schemas.ChatOptionAnalyticsOut,
    summary="How many people selected this option",
    responses={
        403: {"description": "Not the sender of the original message"},
        404: {"description": "Option not found"},
    },
)
def get_option_analytics(
    option_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """`selection_count` = distinct users who selected the option. Only the
    sender of the original message can read it (403 otherwise)."""
    option = _get_option_or_404(db, option_id)
    _require_message_owner(option.message, current_user)
    count = (
        db.query(models.ChatMessageOptionSelection)
        .filter(models.ChatMessageOptionSelection.option_id == option.id)
        .count()
    )
    return schemas.ChatOptionAnalyticsOut(
        option_id=option.id, title=option.title, action=option.action, selection_count=count
    )
