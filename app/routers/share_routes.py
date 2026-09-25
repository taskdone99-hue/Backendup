import os

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user, get_current_user_optional
from app.services.privacy_service import is_blocked
from app.routers.content_routes import _require_author_visible
from app.routers.chat_routes import (
    _get_participant_or_403,
    _conversation_participant_ids,
    _reject_if_blocked_in_conversation,
    _create_and_dispatch_message,
    get_or_create_direct_conversation,
)

router = APIRouter(prefix="/api/share", tags=["share"])

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

CONTENT_MODELS = {
    models.ShareContentType.post: models.Post,
    models.ShareContentType.reel: models.Reel,
}


def _get_content_or_404(db: Session, content_type: models.ShareContentType, content_id: int):
    model = CONTENT_MODELS[content_type]
    obj = db.query(model).filter(model.id == content_id).first()
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{content_type.value.capitalize()} not found",
        )
    return obj


@router.post(
    "/internal", response_model=schemas.InternalShareResponse, status_code=status.HTTP_201_CREATED
)
async def share_internal(
    payload: schemas.InternalShareRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Instagram-style Share sheet: Post/Reel -> Share -> pick person(s)
    and/or group(s) -> Send. This is the single place that turns a share
    into an actual chat message — the same delivery path as sharing a reel
    from inside a thread (POST /api/chat/conversations/{id}/messages with
    `shared_reel_id`), just reachable from the content itself and able to
    fan out to several targets at once. Every recipient gets a rich
    preview card they can tap to open the original post/reel, never a
    raw URL, and gets it over the live chat socket (or a push/notification
    if offline) exactly like any other chat message.

    `recipient_ids` are people: a direct (1:1) thread is reused if one
    exists, or started fresh (with the usual message-request gating and
    auto-intro DM) otherwise. `conversation_ids` are existing threads —
    1:1 or group — to send straight into, for picking a group chat out of
    the share sheet. At least one of the two is required; both may be
    combined in one call.

    Every validation error (missing content, missing recipient/
    conversation, a block in either direction, not being a participant in
    a targeted conversation) is raised before anything is sent, so a
    request either shares to every target or shares to none of them.
    """
    content = _get_content_or_404(db, payload.content_type, payload.content_id)
    # You can only share what you could open yourself — 404 if the author
    # blocked you (either direction), 403 if it's a private account you
    # don't follow. Also doubles as the "deleted content" check: a
    # deleted post/reel is simply gone from the table, so this 404s too.
    _require_author_visible(db, content.user, current_user.id)

    # ---- resolve + validate every target before sending anything ----

    recipients = []
    if payload.recipient_ids:
        recipients = (
            db.query(models.User).filter(models.User.id.in_(payload.recipient_ids)).all()
        )
        found_ids = {u.id for u in recipients}
        missing = [rid for rid in payload.recipient_ids if rid not in found_ids]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recipient(s) not found: {', '.join(str(m) for m in missing)}",
            )
        for recipient in recipients:
            if recipient.id != current_user.id and is_blocked(db, current_user.id, recipient.id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail="Can't message this user"
                )

    existing_conversations = []
    for conversation_id in payload.conversation_ids:
        conversation = (
            db.query(models.Conversation)
            .filter(models.Conversation.id == conversation_id)
            .first()
        )
        if conversation is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Conversation {conversation_id} not found",
            )
        _get_participant_or_403(db, conversation_id, current_user.id)
        _reject_if_blocked_in_conversation(db, conversation_id, current_user.id)
        existing_conversations.append(conversation)

    # ---- everything checked out — materialize targets and send ----

    target_conversations: list[models.Conversation] = []
    for recipient in recipients:
        if recipient.id == current_user.id:
            continue
        conversation, _is_new = get_or_create_direct_conversation(db, current_user, recipient)
        target_conversations.append(conversation)
    target_conversations.extend(existing_conversations)

    is_post = payload.content_type == models.ShareContentType.post
    default_preview = "Sent a post" if is_post else "Sent a reel"
    text = payload.message or default_preview
    preview = text if len(text) <= 80 else text[:77] + "..."

    shares: list[models.Share] = []
    message_outs: list[schemas.MessageOut] = []

    for conversation in target_conversations:
        message = models.Message(
            conversation_id=conversation.id,
            sender_id=current_user.id,
            content=payload.message,
            shared_post_id=payload.content_id if is_post else None,
            shared_reel_id=payload.content_id if not is_post else None,
        )
        message_out = await _create_and_dispatch_message(
            db, conversation.id, message, current_user, preview
        )
        message_outs.append(message_out)

        # One Share log row per recipient actually reached in this
        # conversation, powering the engagement share count (see
        # app/services/engagement.py.shares_count) — same bookkeeping this
        # endpoint always did, now alongside the real chat delivery.
        for uid in _conversation_participant_ids(db, conversation.id):
            if uid == current_user.id:
                continue
            share = models.Share(
                sender_id=current_user.id,
                recipient_id=uid,
                content_type=payload.content_type,
                content_id=payload.content_id,
                message=payload.message,
            )
            db.add(share)
            shares.append(share)

    db.commit()
    for share in shares:
        db.refresh(share)

    return schemas.InternalShareResponse(message="Shared", shares=shares, messages=message_outs)


@router.get("/{post_id}/link", response_model=schemas.ShareLinkResponse)
def get_share_link(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    """Generate a shareable link for a post. Opening it in a browser (or a
    chat app's link preview) is served by GET /p/{id} — see
    public_share_routes.py. Only handed out for content the caller could
    open themselves: 403 for a private account they don't follow, 404 if
    blocked either way."""
    post = _get_content_or_404(db, models.ShareContentType.post, post_id)
    _require_author_visible(db, post.user, current_user.id if current_user else None)

    base = PUBLIC_BASE_URL or "https://app.example.com"
    return schemas.ShareLinkResponse(post_id=post_id, url=f"{base}/p/{post_id}")


@router.get("/reels/{reel_id}/link", response_model=schemas.ReelShareLinkResponse)
def get_reel_share_link(
    reel_id: int,
    db: Session = Depends(get_db),
    current_user: models.User | None = Depends(get_current_user_optional),
):
    """Generate a shareable deep-link URL for a reel — the reels page only
    had the post version of this (get_share_link above), so a reel share
    button had nowhere to fetch its link from. Same visibility rule as the
    post version; the link opens GET /r/{id}."""
    reel = _get_content_or_404(db, models.ShareContentType.reel, reel_id)
    _require_author_visible(db, reel.user, current_user.id if current_user else None)

    base = PUBLIC_BASE_URL or "https://app.example.com"
    return schemas.ReelShareLinkResponse(reel_id=reel_id, url=f"{base}/r/{reel_id}")
