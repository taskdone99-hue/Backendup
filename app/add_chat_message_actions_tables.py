"""Create chat message favourites and pinned-message tables, and add forward source metadata.

Run from the project root:
    python -m app.add_chat_message_actions_tables
"""
from sqlalchemy import inspect, text, Index
from app.database import engine
from app import models


def upgrade():
    models.MessageFavorite.__table__.create(bind=engine, checkfirst=True)
    models.PinnedMessage.__table__.create(bind=engine, checkfirst=True)

    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("messages")}
    if "forwarded_from_message_id" not in columns:
        # SQLite and MySQL both support this additive nullable column.
        with engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE messages ADD COLUMN forwarded_from_message_id INTEGER NULL"
            ))

    Index("ix_messages_forwarded_from_message_id", models.Message.forwarded_from_message_id).create(
        bind=engine, checkfirst=True
    )


if __name__ == "__main__":
    upgrade()
    print("Chat message actions tables/column are ready.")
