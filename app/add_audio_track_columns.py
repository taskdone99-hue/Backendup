"""
One-off, idempotent migration for existing databases: brings the
`audio_tracks` table in sync with models.Audio.

Why this exists
---------------
`audio_tracks` was originally created by Base.metadata.create_all() (it was a
brand-new table at the time). create_all() never ALTERs an existing table, so
when two columns were later added to models.Audio, databases that already had
the table never received them, and every query that selects from
`audio_tracks` (GET /api/search, /api/audio/*, saved audio, reel audio ...)
started failing with:

    Unknown column 'audio_tracks.<column>' in 'field list'

The columns added after the table was first created, and covered here:

    duration_seconds     INT NULL
    created_by_user_id   INT NULL  (+ index, + FK -> users.id)

Both were added to the model in the same change, so a database that was
patched for one of them by hand can still be missing the other. This script
checks each piece separately and only adds what is missing, so it is safe to
run on:
  * a database missing both columns,
  * a database where `duration_seconds` was already added manually,
  * a fully up-to-date database (it then does nothing).

Run once per environment (each run is safe to repeat):
    python -m app.add_audio_track_columns
    python -m app.add_audio_track_columns --dry-run   # print the SQL only

It connects with the same settings as the app itself (app.database.engine).

Keeping this in sync: if you add another column to models.Audio, add it to
AUDIO_TRACK_COLUMNS below. test/test_audio_schema_sync.py fails if the model
and this migration drift apart.
"""

import sys

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

TABLE = "audio_tracks"

# Columns added to models.Audio AFTER the table was first created. Column
# name -> DDL fragment. Keep the types identical to models.Audio.
AUDIO_TRACK_COLUMNS = {
    "duration_seconds": "INT NULL",
    "created_by_user_id": "INT NULL",
}

# Matches what SQLAlchemy generates for `index=True` on the model column.
CREATED_BY_INDEX = "ix_audio_tracks_created_by_user_id"
CREATED_BY_FK = "fk_audio_tracks_created_by_user"


def plan(engine: Engine) -> list[str]:
    """Returns the SQL statements needed to bring `audio_tracks` up to date
    (an empty list if it already is). Read-only: inspects the live schema."""
    inspector = inspect(engine)
    if TABLE not in inspector.get_table_names():
        # Table doesn't exist yet -> create_all() (run at app startup) will
        # build it with the full, current schema. Nothing to migrate.
        return []

    existing_columns = {col["name"] for col in inspector.get_columns(TABLE)}
    statements: list[str] = []

    for name, ddl in AUDIO_TRACK_COLUMNS.items():
        if name not in existing_columns:
            statements.append(f"ALTER TABLE {TABLE} ADD COLUMN {name} {ddl}")

    # Index / FK on created_by_user_id. Detected by the column they cover, not
    # by name, so an index or FK someone already added by hand (under any
    # name) isn't duplicated.
    column_is_new = "created_by_user_id" not in existing_columns
    has_index = any(
        idx["column_names"] and idx["column_names"][0] == "created_by_user_id"
        for idx in inspector.get_indexes(TABLE)
    )
    if column_is_new or not has_index:
        statements.append(f"CREATE INDEX {CREATED_BY_INDEX} ON {TABLE} (created_by_user_id)")

    # SQLite can't ALTER TABLE ... ADD CONSTRAINT, so the FK is MySQL-only
    # (production). On a fresh SQLite schema create_all() already declares it.
    if engine.dialect.name == "mysql":
        has_fk = any(
            fk["constrained_columns"] == ["created_by_user_id"]
            for fk in inspector.get_foreign_keys(TABLE)
        )
        if column_is_new or not has_fk:
            # No ON DELETE clause: matches the model's ForeignKey("users.id"),
            # so a migrated database behaves exactly like a freshly created one.
            statements.append(
                f"ALTER TABLE {TABLE} ADD CONSTRAINT {CREATED_BY_FK} "
                f"FOREIGN KEY (created_by_user_id) REFERENCES users(id)"
            )

    return statements


def migrate(engine: Engine, dry_run: bool = False) -> list[str]:
    """Applies (or, with dry_run, only returns) the statements from plan()."""
    statements = plan(engine)
    if statements and not dry_run:
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))
    return statements


def main() -> None:
    from app.database import engine  # imported lazily: no DB settings needed to import this module

    dry_run = "--dry-run" in sys.argv[1:]
    statements = migrate(engine, dry_run=dry_run)

    if not statements:
        print(f"{TABLE} is already up to date - nothing to do")
    for statement in statements:
        print(("[dry-run] " if dry_run else "applied: ") + statement)
    print("Done")


if __name__ == "__main__":
    main()
