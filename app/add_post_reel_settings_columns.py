"""One-time migration for existing databases. Adds:

    users.hide_like_count   BOOLEAN NOT NULL DEFAULT 0   global "hide like counts"
    users.hide_comments     BOOLEAN NOT NULL DEFAULT 0   global "hide comments"
    posts.editor_state      JSON NULL                    client editor state
    reels.editor_state      JSON NULL                    client editor state

Existing rows are unaffected: every user reads as hiding nothing, and every
post/reel reads back `editor_state: null`. No backfill, nothing dropped or
rewritten. Idempotent (inspects the live schema and only adds what's missing),
so it is safe to run twice, and safe on a database where create_all() already
built the columns. Tables that don't exist yet are skipped (create_all builds
them with the full current schema at app startup).

    python -m app.add_post_reel_settings_columns
    python -m app.add_post_reel_settings_columns --dry-run   # print the SQL only

Keep NEW_COLUMNS in step with models.User / models.Post / models.Reel;
test/test_post_reel_settings.py fails if they drift apart.
"""

import sys

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

# table -> {column: DDL fragment}. Types match the models.
NEW_COLUMNS = {
    "users": {
        "hide_like_count": "BOOLEAN NOT NULL DEFAULT 0",
        "hide_comments": "BOOLEAN NOT NULL DEFAULT 0",
    },
    "posts": {"editor_state": "JSON NULL"},
    "reels": {"editor_state": "JSON NULL"},
}


def plan(engine: Engine) -> list[str]:
    """SQL needed to bring the tables up to date (empty if they already are). Read-only."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    statements = []
    for table, columns in NEW_COLUMNS.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        statements += [
            f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"
            for name, ddl in columns.items()
            if name not in existing
        ]
    return statements


def migrate(engine: Engine, dry_run: bool = False) -> list[str]:
    statements = plan(engine)
    if statements and not dry_run:
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))
    return statements


def main() -> None:
    from app.database import engine  # lazy: importing this module needs no DB settings

    dry_run = "--dry-run" in sys.argv[1:]
    statements = migrate(engine, dry_run=dry_run)
    if not statements:
        print("users / posts / reels are already up to date - nothing to do")
    for statement in statements:
        print(("[dry-run] " if dry_run else "applied: ") + statement)
    print("Done")


if __name__ == "__main__":
    main()
