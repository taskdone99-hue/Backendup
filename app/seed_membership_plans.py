"""
One-off seed for default membership plans, so GET /api/membership/plans
has something to return out of the box. Safe to re-run — it skips plans
that already exist by name.

Run once (after the app has started at least once, so the table exists):
    python -m app.seed_membership_plans
"""

from app.database import SessionLocal
from app.models import MembershipInterval, MembershipPlan

DEFAULT_PLANS = [
    {
        "name": "Plus",
        "description": "Ad-light experience, badge on profile, priority support.",
        "price_amount": 9900,  # smallest currency unit, i.e. ₹99.00
        "currency": "INR",
        "interval": MembershipInterval.monthly,
    },
    {
        "name": "Pro",
        "description": "No ads, revenue-share eligibility on reels, analytics dashboard.",
        "price_amount": 29900,  # ₹299.00
        "currency": "INR",
        "interval": MembershipInterval.monthly,
    },
    {
        "name": "Pro (Annual)",
        "description": "Everything in Pro, billed yearly at a discount.",
        "price_amount": 299900,  # ₹2,999.00
        "currency": "INR",
        "interval": MembershipInterval.yearly,
    },
]


def run() -> None:
    db = SessionLocal()
    try:
        for plan_data in DEFAULT_PLANS:
            existing = db.query(MembershipPlan).filter(MembershipPlan.name == plan_data["name"]).first()
            if existing is not None:
                print(f"skip (exists): {plan_data['name']}")
                continue
            db.add(MembershipPlan(**plan_data, is_active=True))
            print(f"added: {plan_data['name']}")
        db.commit()
    finally:
        db.close()
    print("Done")


if __name__ == "__main__":
    run()
