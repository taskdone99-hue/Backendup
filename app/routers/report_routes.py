"""
Reporting + moderation. Two routers in one file since they're one
feature split by audience:
  - `router`       (/api/reports)       — submit a report, see your own.
  - `admin_router`  (/api/admin/reports) — review queue, gated by
    get_current_admin_user (models.User.is_admin — see auth.py). A normal
    user hitting anything under /api/admin gets a 403, never a peek at the
    data (FastAPI evaluates the dependency before the route body runs).
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user, get_current_admin_user
from app.services import moderation_service

router = APIRouter(prefix="/api/reports", tags=["reports"])
admin_router = APIRouter(prefix="/api/admin/reports", tags=["admin"])


def _get_own_report_or_404(db: Session, report_id: int, current_user: models.User) -> models.Report:
    report = (
        db.query(models.Report)
        .filter(models.Report.id == report_id, models.Report.reporter_id == current_user.id)
        .first()
    )
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    return report


# ==========================================================================
# User-facing
# ==========================================================================

@router.post("", response_model=schemas.ReportOut, status_code=status.HTTP_201_CREATED)
def create_report(
    payload: schemas.ReportCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    moderation_service.validate_report_target(db, payload.target_type, payload.target_id)

    if payload.target_type == models.ReportTargetType.user and payload.target_id == current_user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You can't report yourself")

    # Not unique-constrained: the same person reporting the same target
    # twice for two different reasons is legitimate (e.g. spam, then later
    # harassment) — but an exact duplicate (same reason too) while the
    # first is still open is almost always a double-tap, so just hand back
    # the existing one instead of creating a near-identical row.
    existing = (
        db.query(models.Report)
        .filter(
            models.Report.reporter_id == current_user.id,
            models.Report.target_type == payload.target_type,
            models.Report.target_id == payload.target_id,
            models.Report.reason == payload.reason,
            models.Report.status.in_([models.ReportStatus.pending, models.ReportStatus.reviewing]),
        )
        .first()
    )
    if existing is not None:
        return schemas.ReportOut.model_validate(existing)

    report = models.Report(
        reporter_id=current_user.id,
        target_type=payload.target_type,
        target_id=payload.target_id,
        reason=payload.reason,
        description=payload.description,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return schemas.ReportOut.model_validate(report)


@router.get("/mine", response_model=schemas.PaginatedReportsResponse)
def get_my_reports(
    status_filter: models.ReportStatus | None = Query(default=None, alias="status"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    query = db.query(models.Report).filter(models.Report.reporter_id == current_user.id)
    if status_filter is not None:
        query = query.filter(models.Report.status == status_filter)

    total = query.count()
    reports = query.order_by(models.Report.created_at.desc()).offset(offset).limit(limit).all()
    items = [schemas.ReportOut.model_validate(r) for r in reports]
    return schemas.PaginatedReportsResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/{report_id}", response_model=schemas.ReportOut)
def get_my_report(
    report_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    report = _get_own_report_or_404(db, report_id, current_user)
    return schemas.ReportOut.model_validate(report)


# ==========================================================================
# Admin
# ==========================================================================

@admin_router.get("", response_model=schemas.PaginatedAdminReportsResponse)
def admin_list_reports(
    status_filter: models.ReportStatus | None = Query(default=None, alias="status"),
    target_type: models.ReportTargetType | None = Query(default=None),
    reason: models.ReportReason | None = Query(default=None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _admin: models.User = Depends(get_current_admin_user),
):
    query = db.query(models.Report)
    if status_filter is not None:
        query = query.filter(models.Report.status == status_filter)
    if target_type is not None:
        query = query.filter(models.Report.target_type == target_type)
    if reason is not None:
        query = query.filter(models.Report.reason == reason)

    total = query.count()
    reports = query.order_by(models.Report.created_at.desc()).offset(offset).limit(limit).all()
    items = [schemas.AdminReportOut.model_validate(r) for r in reports]
    return schemas.PaginatedAdminReportsResponse(total=total, limit=limit, offset=offset, items=items)


@admin_router.get("/{report_id}", response_model=schemas.AdminReportOut)
def admin_get_report(
    report_id: int,
    db: Session = Depends(get_db),
    _admin: models.User = Depends(get_current_admin_user),
):
    report = db.query(models.Report).filter(models.Report.id == report_id).first()
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    return schemas.AdminReportOut.model_validate(report)


@admin_router.put("/{report_id}", response_model=schemas.AdminReportOut)
async def admin_update_report(
    report_id: int,
    payload: schemas.ReportStatusUpdate,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin_user),
):
    report = db.query(models.Report).filter(models.Report.id == report_id).first()
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")

    report.status = payload.status
    if payload.action is not None:
        await moderation_service.apply_action(db, report, payload.action, admin)
    report.reviewed_by_id = admin.id
    report.reviewed_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(report)
    return schemas.AdminReportOut.model_validate(report)
