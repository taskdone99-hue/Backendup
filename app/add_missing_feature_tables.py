"""Add chat expiry/theme, not-interested reasons, video engagement and analytics events.
Run: python -m app.add_missing_feature_tables
Safe to rerun. Back up the database before running on a deployed environment.
"""
from sqlalchemy import inspect, text
from app.database import Base, engine
from app import models  # noqa: F401

NEW_TABLES = [
    models.ConversationDisappearingSetting.__table__,
    models.ConversationThemePreference.__table__,
    models.VideoComment.__table__,
    models.VideoLike.__table__,
    models.ContentInteractionEvent.__table__,
]

def main():
    Base.metadata.create_all(bind=engine, tables=NEW_TABLES, checkfirst=True)
    inspector = inspect(engine)
    dialect = engine.dialect.name
    with engine.begin() as conn:
        message_columns = {c["name"] for c in inspector.get_columns("messages")}
        if "expires_at" not in message_columns:
            conn.execute(text("ALTER TABLE messages ADD COLUMN expires_at DATETIME NULL"))
        ni_columns = {c["name"] for c in inspector.get_columns("not_interested")}
        if "reason" not in ni_columns:
            if dialect == "sqlite":
                conn.execute(text("ALTER TABLE not_interested ADD COLUMN reason VARCHAR(30) NOT NULL DEFAULT 'not_relevant'"))
            else:
                conn.execute(text("ALTER TABLE not_interested ADD COLUMN reason VARCHAR(30) NOT NULL DEFAULT 'not_relevant'"))
    print("Missing feature tables and columns are ready.")

if __name__ == "__main__":
    main()
