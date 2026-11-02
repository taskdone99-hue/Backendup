"""Create the additive Reel insight event table on an existing database.

Run from the project root: python -m app.add_reel_insight_events_table
Safe to rerun; existing tables are left untouched.
"""
from app.database import engine
from app.models import ReelInsightEvent


def main() -> None:
    ReelInsightEvent.__table__.create(bind=engine, checkfirst=True)
    print("reel_insight_events table is present")


if __name__ == "__main__":
    main()
