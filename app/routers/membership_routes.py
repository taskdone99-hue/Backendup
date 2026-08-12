from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user

router = APIRouter(prefix="/api/membership", tags=["membership"])

_PERIOD_BY_INTERVAL = {
    models.MembershipInterval.monthly: timedelta(days=30),
    models.MembershipInterval.yearly: timedelta(days=365),
}


@router.get("/plans", response_model=schemas.MembershipPlansResponse)
def get_membership_plans(db: Session = Depends(get_db)):
    plans = (
        db.query(models.MembershipPlan)
        .filter(models.MembershipPlan.is_active.is_(True))
        .order_by(models.MembershipPlan.price_amount.asc())
        .all()
    )
    return schemas.MembershipPlansResponse(plans=plans)


@router.post("/subscribe", response_model=schemas.SubscribeResponse, status_code=status.HTTP_201_CREATED)
def subscribe_to_plan(
    payload: schemas.SubscribeRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    plan = (
        db.query(models.MembershipPlan)
        .filter(models.MembershipPlan.id == payload.plan_id, models.MembershipPlan.is_active.is_(True))
        .first()
    )
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membership plan not found")

    # If a payment order was supplied, it must belong to this user and be
    # marked paid before we'll activate the subscription.
    if payload.payment_order_id is not None:
        order = (
            db.query(models.PaymentOrder)
            .filter(
                models.PaymentOrder.id == payload.payment_order_id,
                models.PaymentOrder.user_id == current_user.id,
            )
            .first()
        )
        if order is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment order not found")
        if order.status != models.PaymentStatus.paid:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="Payment for this order hasn't been confirmed yet",
            )

    now = datetime.now(timezone.utc)
    period_end = now + _PERIOD_BY_INTERVAL.get(plan.interval, timedelta(days=30))

    membership = (
        db.query(models.UserMembership)
        .filter(models.UserMembership.user_id == current_user.id)
        .first()
    )
    if membership is None:
        membership = models.UserMembership(user_id=current_user.id)
        db.add(membership)

    membership.plan_id = plan.id
    membership.status = models.MembershipStatus.active
    membership.current_period_start = now
    membership.current_period_end = period_end
    membership.payment_order_id = payload.payment_order_id

    db.commit()
    db.refresh(membership)
    membership = (
        db.query(models.UserMembership)
        .options(joinedload(models.UserMembership.plan))
        .filter(models.UserMembership.id == membership.id)
        .first()
    )

    return schemas.SubscribeResponse(message="Subscribed successfully", membership=membership)


@router.get("/status", response_model=schemas.MembershipStatusResponse)
def get_membership_status(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    membership = (
        db.query(models.UserMembership)
        .options(joinedload(models.UserMembership.plan))
        .filter(models.UserMembership.user_id == current_user.id)
        .first()
    )
    if membership is None:
        return schemas.MembershipStatusResponse(is_member=False, membership=None)

    now = datetime.now(timezone.utc)
    period_end = membership.current_period_end
    if period_end is not None and period_end.tzinfo is None:
        period_end = period_end.replace(tzinfo=timezone.utc)
    is_expired = period_end is not None and period_end < now
    if is_expired and membership.status == models.MembershipStatus.active:
        membership.status = models.MembershipStatus.expired
        db.commit()
        db.refresh(membership)

    is_member = membership.status == models.MembershipStatus.active and not is_expired
    return schemas.MembershipStatusResponse(is_member=is_member, membership=membership)
