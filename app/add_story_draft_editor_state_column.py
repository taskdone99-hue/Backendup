"""
One-off, idempotent migration for existing databases: adds the nullable JSON
`editor_state` column to `story_drafts` (models.StoryDraft.editor_state).

Why this exists
---------------
`story_drafts` was created by Base.metadata.create_all(). create_all() never
ALTERs an existing table, so databases that already have the table will not
get the new column on their own - and every query that selects from
`story_drafts` (list / get / publish / delete draft) would then fail with:

    Unknown column 'story_drafts.editor_state' in 'field list'

Run this once per environment BEFORE (or together with) deploying the code
that reads the column.

What it does
------------
    ALTER TABLE story_drafts ADD COLUMN editor_state JSON NULL

* The column is nullable with no default, so every existing draft keeps
  working untouched and reads back as `editor_state: null`. No backfill.
* Native JSON type on MySQL (5.7.8+ / 8.x - what production runs). MySQL has
  no JSONB; that is a PostgreSQL type.
* Safe to repeat: it only adds the column if it is missing, and does nothing
  if the table doesn't exist yet (create_all() at app startup builds it with
  the column included).

Run once per environment (each run is safe to repeat):
    python -m app.add_story_draft_editor_state_column
    python -m app.add_story_draft_editor_state_column --dry-run   # print the SQL only

It connects with the same settings as the app itself (app.database.engine).

Keeping this in sync: if you add another column to models.StoryDraft, add it
to STORY_DRAFT_COLUMNS below. test/test_story_draft_editor_state.py fails if
the model and this migration drift apart.
"""

import sys

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

TABLE = "story_drafts"

# Columns added to models.StoryDraft AFTER the table was first created.
# Column name -> DDL fragment. Keep the types identical to models.StoryDraft.
STORY_DRAFT_COLUMNS = {
    "editor_state": "JSON NULL",
}


def plan(engine: Engine) -> list[str]:
    """Returns the SQL statements needed to bring `story_drafts` up to date
    (an empty list if it already is). Read-only: inspects the live schema."""
    inspector = inspect(engine)
    if TABLE not in inspector.get_table_names():
        # Table doesn't exist yet -> create_all() (run at app startup) will
        # build it with the full, current schema. Nothing to migrate.
        return []

    existing_columns = {col["name"] for col in inspector.get_columns(TABLE)}
    return [
        f"ALTER TABLE {TABLE} ADD COLUMN {name} {ddl}"
        for name, ddl in STORY_DRAFT_COLUMNS.items()
        if name not in existing_columns
    ]


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
