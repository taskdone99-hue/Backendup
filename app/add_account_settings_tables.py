"""Create account settings tables and add global engagement flags.

Run from the project root: python -m app.add_account_settings_tables
Idempotent for the new tables and user columns. Back up the database first.
"""
from sqlalchemy import inspect, text
from app.database import Base, engine
from app import models  # noqa: F401 - register models

TABLES = [
    models.UserCommentSettings.__table__,
    models.UserMentionSettings.__table__,
    models.UserHiddenWord.__table__,
    models.UserScreenTimeSession.__table__,
]
Base.metadata.create_all(bind=engine, tables=TABLES, checkfirst=True)

inspector = inspect(engine)
columns = {c["name"] for c in inspector.get_columns("users")}
with engine.begin() as conn:
    if "hide_share_count" not in columns:
        conn.execute(text("ALTER TABLE users ADD COLUMN hide_share_count BOOLEAN NOT NULL DEFAULT 0"))
    if "hide_saved_count" not in columns:
        conn.execute(text("ALTER TABLE users ADD COLUMN hide_saved_count BOOLEAN NOT NULL DEFAULT 0"))
print("Account settings tables and engagement columns are ready.")
