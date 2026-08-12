import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user
from app.services import payment_service

router = APIRouter(prefix="/api/payments", tags=["payments"])

logger = logging.getLogger(__name__)


@router.post("/create-order", response_model=schemas.CreateOrderResponse, status_code=status.HTTP_201_CREATED)
def create_order(
    payload: schemas.CreateOrderRequest,
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

    receipt = f"user{current_user.id}_plan{plan.id}_{secrets.token_hex(4)}"
    provider_order_id, provider_key = payment_service.create_order(
        provider=payload.provider,
        amount=plan.price_amount,
        currency=plan.currency,
        receipt=receipt,
    )

    order = models.PaymentOrder(
        user_id=current_user.id,
        plan_id=plan.id,
        provider=payload.provider,
        provider_order_id=provider_order_id,
        amount=plan.price_amount,
        currency=plan.currency,
        status=models.PaymentStatus.created,
        receipt=receipt,
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    return schemas.CreateOrderResponse(
        order_id=order.provider_order_id,
        amount=order.amount,
        currency=order.currency,
        provider=order.provider,
        provider_key=provider_key,
        status=order.status,
    )


@router.post("/webhook", response_model=schemas.PaymentWebhookResponse)
async def payment_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_razorpay_signature: str | None = Header(default=None),
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
):
    """Provider-agnostic webhook endpoint. Expects a JSON body containing at
    least `provider`, `order_id`, and `event` (e.g. "payment.captured" /
    "payment.failed"). Real Razorpay/Stripe payloads differ in shape — once
    a real SDK is wired in, translate their payload into this shape (or add
    a provider-specific parse branch) before calling this handler's logic.
    """
    raw_body = await request.body()
    payload = await request.json()

    provider_str = payload.get("provider", "razorpay")
    try:
        provider = models.PaymentProvider(provider_str)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown payment provider")

    signature = x_razorpay_signature if provider == models.PaymentProvider.razorpay else stripe_signature
    if not payment_service.verify_webhook_signature(provider, raw_body, signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")

    order_id = payload.get("order_id")
    event = payload.get("event", "")

    order = (
        db.query(models.PaymentOrder)
        .filter(models.PaymentOrder.provider_order_id == order_id)
        .first()
    )
    if order is None:
        logger.info("[Payment webhook] unknown order_id=%s event=%s", order_id, event)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    if "captured" in event or "succeeded" in event or event == "payment.paid":
        order.status = models.PaymentStatus.paid
        order.paid_at = datetime.now(timezone.utc)
    elif "failed" in event:
        order.status = models.PaymentStatus.failed
    elif "refund" in event:
        order.status = models.PaymentStatus.refunded

    db.commit()

    return schemas.PaymentWebhookResponse(message="Webhook processed", order_id=order_id, status=order.status)
