"""One-time migration for existing databases: adds the video upload-page
settings columns to `reels`.

    language          VARCHAR(50)  NULL                 (never set = NULL)
    ai_generated      BOOLEAN      NOT NULL DEFAULT 0
    comments_enabled  BOOLEAN      NOT NULL DEFAULT 1
    hide_like_count   BOOLEAN      NOT NULL DEFAULT 0
    hide_comments     BOOLEAN      NOT NULL DEFAULT 0
    audience          VARCHAR(20)  NULL                 (never set = NULL)
    visibility        VARCHAR(20)  NOT NULL DEFAULT 'public'
    schedule_enabled  BOOLEAN      NOT NULL DEFAULT 0
    scheduled_at      DATETIME     NULL                 (naive UTC)

plus indexes on `visibility` and `scheduled_at`.

Existing rows pick up the defaults, i.e. every existing reel stays public,
comments on, nothing hidden, no schedule — exactly how it behaved before.

Idempotent: it inspects the table first and only adds what is missing, so it
is safe to run twice (and on a database where create_all() already made the
columns because the table was new).

Run once from the project root:
    python -m app.add_video_upload_settings_columns
"""

from sqlalchemy import inspect, text

# (column name, DDL type/default clause) — one ALTER per column.
COLUMNS = [
    ("language", "VARCHAR(50) NULL"),
    ("ai_generated", "BOOLEAN NOT NULL DEFAULT 0"),
    ("comments_enabled", "BOOLEAN NOT NULL DEFAULT 1"),
    ("hide_like_count", "BOOLEAN NOT NULL DEFAULT 0"),
    ("hide_comments", "BOOLEAN NOT NULL DEFAULT 0"),
    ("audience", "VARCHAR(20) NULL"),
    ("visibility", "VARCHAR(20) NOT NULL DEFAULT 'public'"),
    ("schedule_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
    ("scheduled_at", "DATETIME NULL"),
]

INDEXES = [
    ("ix_reels_visibility", "visibility"),
    ("ix_reels_scheduled_at", "scheduled_at"),
]


def migrate(engine) -> list[str]:
    """Applies the migration to `engine`; returns what it did (for logging/tests)."""
    done: list[str] = []
    existing_columns = {c["name"] for c in inspect(engine).get_columns("reels")}
    with engine.begin() as conn:
        for name, ddl in COLUMNS:
            if name in existing_columns:
                continue
            conn.execute(text(f"ALTER TABLE reels ADD COLUMN {name} {ddl}"))
            done.append(f"reels.{name} added")

    existing_indexes = {i["name"] for i in inspect(engine).get_indexes("reels")}
    with engine.begin() as conn:
        for index_name, column in INDEXES:
            if index_name in existing_indexes:
                continue
            conn.execute(text(f"CREATE INDEX {index_name} ON reels ({column})"))
            done.append(f"{index_name} created")
    return done


def main() -> None:
    from app.database import engine

    done = migrate(engine)
    for line in done:
        print(line)
    print("Video upload settings migration completed." if done else "Nothing to do — already migrated.")


if __name__ == "__main__":
    main()
