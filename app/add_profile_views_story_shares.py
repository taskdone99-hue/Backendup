"""Add profile-view tracking and story share references.

Run with:
    python -m app.add_profile_views_story_shares
The migration is idempotent and uses the configured SQLAlchemy connection (including DB TLS).
"""
from sqlalchemy import inspect, text
from app.database import engine
from app import models

inspector = inspect(engine)
with engine.begin() as conn:
    # New table; safe if it already exists.
    models.ProfileView.__table__.create(bind=conn, checkfirst=True)

    tables = set(inspect(conn).get_table_names())
    for table, column, ddl in [
        ("stories", "source_story_id", "ALTER TABLE stories ADD COLUMN source_story_id INTEGER NULL"),
        ("messages", "shared_story_id", "ALTER TABLE messages ADD COLUMN shared_story_id INTEGER NULL"),
    ]:
        if table not in tables:
            raise RuntimeError(f"Required table {table!r} does not exist")
        existing = {c["name"] for c in inspect(conn).get_columns(table)}
        if column not in existing:
            conn.execute(text(ddl))
            print(f"Added {table}.{column}")
        else:
            print(f"{table}.{column} already exists")

print("Profile-view and story-share migration complete.")
