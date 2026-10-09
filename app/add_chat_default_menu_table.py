"""One-time migration for existing databases: creates `chat_default_menus`,
the per-account saved menu that is sent automatically when a customer messages
a Business / Premium account.

    chat_default_menus   id, user_id (FK users, UNIQUE, ON DELETE CASCADE),
                         is_enabled (default false), greeting (NULL = default text),
                         options (JSON list of {title, action}), created_at, updated_at

Create-only and idempotent (checkfirst): never alters, drops or rewrites an
existing table, touches no existing row. The app's startup create_all() would
also create it, in which case this is a no-op.

Run once from the project root:
    python -m app.add_chat_default_menu_table
"""

from sqlalchemy import inspect

from app import models

TABLES = [models.ChatDefaultMenu.__table__]


def migrate(engine) -> list[str]:
    existing = set(inspect(engine).get_table_names())
    created = [t.name for t in TABLES if t.name not in existing]
    models.Base.metadata.create_all(bind=engine, tables=TABLES, checkfirst=True)
    return [f"{name} created" for name in created]


def main() -> None:
    from app.database import engine

    done = migrate(engine)
    for line in done:
        print(line)
    print("Default menu migration completed." if done else "Nothing to do - table already exists.")


if __name__ == "__main__":
    main()
