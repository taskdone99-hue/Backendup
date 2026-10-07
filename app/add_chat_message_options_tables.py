"""One-time migration for existing databases: creates the two tables behind
interactive chat options (Business / Premium quick-reply buttons).

    chat_message_options             id, message_id (FK messages, ON DELETE CASCADE),
                                     title, action, display_order, is_enabled,
                                     created_at, updated_at;
                                     UNIQUE (message_id, action)
    chat_message_option_selections   id, option_id (FK options, ON DELETE CASCADE),
                                     user_id (FK users), selected_at;
                                     UNIQUE (option_id, user_id)

Create-only and idempotent: it uses CREATE TABLE IF NOT EXISTS semantics
(checkfirst), never alters, drops or rewrites an existing table, and touches
no existing row. `messages` and `users` must already exist (they do). Note the
app's own startup create_all() would also create these tables, so on a
database the app has already started against this is a no-op.

Run once from the project root:
    python -m app.add_chat_message_options_tables
"""

from sqlalchemy import inspect

from app import models

TABLES = [
    models.ChatMessageOption.__table__,
    models.ChatMessageOptionSelection.__table__,
]


def migrate(engine) -> list[str]:
    """Creates whichever of the tables are missing; returns what it created."""
    existing = set(inspect(engine).get_table_names())
    created = [t.name for t in TABLES if t.name not in existing]
    models.Base.metadata.create_all(bind=engine, tables=TABLES, checkfirst=True)
    return [f"{name} created" for name in created]


def main() -> None:
    from app.database import engine

    done = migrate(engine)
    for line in done:
        print(line)
    print("Chat options migration completed." if done else "Nothing to do — tables already exist.")


if __name__ == "__main__":
    main()
