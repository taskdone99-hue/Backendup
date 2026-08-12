import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user
from app.services import discord_service

router = APIRouter(prefix="/api/discord", tags=["discord"])

logger = logging.getLogger(__name__)


@router.get("/server-stats", response_model=schemas.DiscordServerStatsOut)
def get_server_stats():
    stats = discord_service.get_server_stats()
    return schemas.DiscordServerStatsOut(**stats)


@router.post("/link-account", response_model=schemas.DiscordLinkResponse, status_code=status.HTTP_201_CREATED)
def link_discord_account(
    payload: schemas.DiscordLinkRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    existing_for_discord_id = (
        db.query(models.DiscordLink)
        .filter(models.DiscordLink.discord_user_id == payload.discord_user_id)
        .first()
    )
    if existing_for_discord_id is not None and existing_for_discord_id.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This Discord account is already linked to another user",
        )

    link = (
        db.query(models.DiscordLink)
        .filter(models.DiscordLink.user_id == current_user.id)
        .first()
    )
    if link is None:
        link = models.DiscordLink(user_id=current_user.id)
        db.add(link)

    link.discord_user_id = payload.discord_user_id
    link.discord_username = payload.discord_username

    db.commit()
    db.refresh(link)

    return schemas.DiscordLinkResponse(
        message="Discord account linked",
        discord_user_id=link.discord_user_id,
        discord_username=link.discord_username,
        linked_at=link.linked_at,
    )


@router.post("/webhook", response_model=schemas.DiscordWebhookResponse)
async def discord_webhook(payload: dict):
    """Receives Discord interaction/event webhooks. Discord's own
    ping/verification handshake (type == 1) needs a bare `{"type": 1}`
    reply signed with Ed25519 — wire that up here once a bot application
    (with a public key) is registered, before pointing Discord's dashboard
    at this URL in production.
    """
    event = payload.get("type") or payload.get("event")
    logger.info("[Discord webhook] received event=%s", event)
    return schemas.DiscordWebhookResponse(message="Webhook received", event=str(event) if event is not None else None)
