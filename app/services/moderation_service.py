"""
Backs PUT /api/admin/reports/{id} — validating a report's target exists
(same "six tables, one generic target_id, router validates" shape as
Like/Mention) and applying the optional side-effecting action.

See models.ReportAction for what each action means and why
"restore_content" isn't offered — remove_content is a real hard delete,
same as this codebase's other delete endpoints, so there's nothing to
restore from.
"""

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app import models
from app.services.notification_service import notify_user

# Maps a report's target_type to the model class that owns the row, so
# both "does this target exist" (report creation) and "delete this
# target" (remove_content action) can be one small dispatch table instead
# of a six-way if/elif in each caller.
_TARGET_MODELS = {
    models.ReportTargetType.user: models.User,
    models.ReportTargetType.post: models.Post,
    models.ReportTargetType.reel: models.Reel,
    models.ReportTargetType.comment: models.Comment,
    models.ReportTargetType.story: models.Story,
    models.ReportTargetType.message: models.Message,
}


def get_report_target(db: Session, target_type: models.ReportTargetType, target_id: int):
    model = _TARGET_MODELS[target_type]
    return db.query(model).filter(model.id == target_id).first()


def validate_report_target(db: Session, target_type: models.ReportTargetType, target_id: int) -> None:
    if get_report_target(db, target_type, target_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{target_type.value.capitalize()} {target_id} not found",
        )


def _target_owner_id(target_type: models.ReportTargetType, target) -> int | None:
    if target_type == models.ReportTargetType.user:
        return target.id
    if target_type == models.ReportTargetType.message:
        return target.sender_id
    return getattr(target, "user_id", None)


async def apply_action(
    db: Session,
    report: models.Report,
    action: models.ReportAction,
    admin: models.User,
) -> None:
    """Mutates `report` and, for the side-effecting actions, the target
    content/user too. Caller commits afterward (same convention as
    mention_service/hashtag_service — this function stages changes, it
    doesn't commit)."""
    report.action = action

    if action in (models.ReportAction.dismiss, models.ReportAction.resolve):
        return

    target = get_report_target(db, report.target_type, report.target_id)
    if target is None:
        # Already gone (e.g. a second report on content someone else's
        # action already removed) — the status/action change above still
        # applies, there's just nothing left to act on.
        return

    owner_id = _target_owner_id(report.target_type, target)

    if action == models.ReportAction.remove_content:
        if report.target_type == models.ReportTargetType.user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="remove_content doesn't apply to a user report — use suspend_user",
            )
        db.delete(target)

    elif action == models.ReportAction.warn_user:
        if owner_id is not None:
            await notify_user(
                db,
                user_id=owner_id,
                actor=admin,
                notif_type=models.NotificationType.moderation_warning,
                message="You've received a warning for violating our community guidelines",
                target_type=report.target_type.value,
                target_id=report.target_id,
            )

    elif action == models.ReportAction.suspend_user:
        suspend_target_id = target.id if report.target_type == models.ReportTargetType.user else owner_id
        if suspend_target_id is not None:
            user = db.query(models.User).filter(models.User.id == suspend_target_id).first()
            if user is not None:
                user.is_suspended = True
                db.query(models.RefreshToken).filter(
                    models.RefreshToken.user_id == user.id,
                    models.RefreshToken.revoked == False,
                ).update({"revoked": True})
