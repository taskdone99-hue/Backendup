"""
Interactive chat options (WhatsApp-Business-style quick-reply buttons).

Eligibility is decided here, on the backend, from data the server owns —
never from a client-supplied flag:

  * Business  -> User.account_type == AccountType.business
  * Premium   -> an active, unexpired UserMembership (the same rule
                 GET /api/membership/status uses to compute `is_member`)

Either one is enough; having both is fine.
"""

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app import models

MAX_OPTIONS_PER_MESSAGE = 5
TITLE_MAX_LENGTH = 24  # WhatsApp list-row title limit
ACTION_MAX_LENGTH = 50

# ---- default menu (automatic first-contact menu) ----
DEFAULT_GREETING = "How can we help you?"
GREETING_MAX_LENGTH = 1000
# A conversation counts as "inactive" — so the menu is sent again — when
# nothing a person wrote in it (auto-messages don't count) is newer than this.
DEFAULT_MENU_INACTIVITY_HOURS = 24


def is_business_user(user: models.User) -> bool:
    return user.account_type == models.AccountType.business


def has_active_premium(db: Session, user: models.User) -> bool:
    membership = (
        db.query(models.UserMembership)
        .filter(models.UserMembership.user_id == user.id)
        .first()
    )
    if membership is None or membership.status != models.MembershipStatus.active:
        return False
    period_end = membership.current_period_end
    if period_end is None:
        return True
    if period_end.tzinfo is None:
        period_end = period_end.replace(tzinfo=timezone.utc)
    return period_end >= datetime.now(timezone.utc)


def is_eligible_sender(db: Session, user: models.User) -> bool:
    return is_business_user(user) or has_active_premium(db, user)


def require_eligible_sender(db: Session, user: models.User) -> None:
    if not is_eligible_sender(db, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Interactive chat options are available to Business and Premium accounts only",
        )


def visible_options(message: models.Message, viewer_id: int | None) -> list[models.ChatMessageOption]:
    """What `viewer_id` gets in `options`: the sender sees every option (so they
    can re-enable a disabled one); everyone else sees only enabled ones. A
    deleted message shows none."""
    if message.is_deleted:
        return []
    if viewer_id is not None and viewer_id == message.sender_id:
        return list(message.options)
    return [o for o in message.options if o.is_enabled]


def find_duplicate(
    options: list[models.ChatMessageOption],
    title: str,
    action: str,
    exclude_id: int | None = None,
) -> str | None:
    """Within one message, a title or an action may appear only once
    (case-insensitive). Returns a client-readable reason, or None."""
    for o in options:
        if o.id == exclude_id:
            continue
        if o.title.casefold() == title.casefold():
            return f"This message already has an option titled '{o.title}'"
        if o.action.casefold() == action.casefold():
            return f"This message already has an option with action '{o.action}'"
    return None


# ---------------------------------------------------------------------------
# Default menu — sent automatically to a customer who messages the business
# ---------------------------------------------------------------------------

def get_default_menu(db: Session, user_id: int) -> models.ChatDefaultMenu | None:
    return (
        db.query(models.ChatDefaultMenu)
        .filter(models.ChatDefaultMenu.user_id == user_id)
        .first()
    )


def effective_greeting(menu: models.ChatDefaultMenu | None) -> str:
    return (menu.greeting if menu is not None and menu.greeting else DEFAULT_GREETING)


def active_default_menu(db: Session, business: models.User) -> models.ChatDefaultMenu | None:
    """The menu to send on behalf of `business`, or None. Needs ALL of: the
    account is *currently* Business/active-Premium (a lapsed account stops
    sending), the menu is enabled, and it has at least one option."""
    if not is_eligible_sender(db, business):
        return None
    menu = get_default_menu(db, business.id)
    if menu is None or not menu.is_enabled or not menu.options:
        return None
    return menu


def conversation_is_inactive(db: Session, conversation_id: int, before_message_id: int) -> bool:
    """True if, before message `before_message_id`, nobody had written in this
    conversation within DEFAULT_MENU_INACTIVITY_HOURS (or ever).

    Automatic messages (the account's welcome DM, earlier menus) and deleted
    messages are ignored, so a brand-new thread — which already holds the
    business's auto-intro — still counts as first contact."""
    previous = (
        db.query(models.Message)
        .filter(
            models.Message.conversation_id == conversation_id,
            models.Message.id < before_message_id,
            models.Message.is_deleted.is_(False),
            models.Message.is_auto_message.is_(False),
        )
        .order_by(models.Message.id.desc())
        .first()
    )
    if previous is None or previous.created_at is None:
        return True
    created = previous.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created < datetime.now(timezone.utc) - timedelta(hours=DEFAULT_MENU_INACTIVITY_HOURS)
