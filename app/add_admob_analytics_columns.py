"""
One-off migration for existing databases, required by the AdMob backend
switch (Ads analytics):

  * ad_impressions: adds `platform` and `ad_unit_id`, makes `ad_id` nullable
    (AdMob ads have no server-side ad id), and indexes `placement`.
  * ad_clicks: created if missing (create_all() in app.main also creates it,
    but running this first keeps the deploy order simple).

Idempotent — safe to run more than once. MySQL only, like the other
add_*.py scripts. Run this BEFORE deploying the new code: the new
impression insert writes the new columns.

Run once:
    python -m app.add_admob_analytics_columns
"""

from sqlalchemy import inspect, text

from app import models
from app.database import engine


def main() -> None:
    inspector = inspect(engine)

    if inspector.has_table("ad_impressions"):
        columns = {c["name"] for c in inspector.get_columns("ad_impressions")}
        indexes = {i["name"] for i in inspector.get_indexes("ad_impressions")}
        with engine.begin() as conn:
            if "platform" not in columns:
                conn.execute(text("ALTER TABLE ad_impressions ADD COLUMN platform VARCHAR(10) NULL"))
                print("ad_impressions.platform added")
            if "ad_unit_id" not in columns:
                conn.execute(text("ALTER TABLE ad_impressions ADD COLUMN ad_unit_id VARCHAR(100) NULL"))
                print("ad_impressions.ad_unit_id added")
            # MODIFY is a no-op when the column is already nullable.
            conn.execute(text("ALTER TABLE ad_impressions MODIFY ad_id VARCHAR(100) NULL"))
            print("ad_impressions.ad_id is now nullable")
            if "ix_ad_impressions_placement" not in indexes:
                conn.execute(text("CREATE INDEX ix_ad_impressions_placement ON ad_impressions (placement)"))
                print("ix_ad_impressions_placement added")
    else:
        # Fresh database: create_all() builds the whole table with the new shape.
        models.AdImpression.__table__.create(bind=engine, checkfirst=True)
        print("ad_impressions created")

    models.AdClick.__table__.create(bind=engine, checkfirst=True)
    print("ad_clicks ready")
    print("Done")


if __name__ == "__main__":
    main()
