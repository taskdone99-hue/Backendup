"""Idempotent migration for reel audience and engagement controls.

Adds age_restriction, remixing_enabled and downloads_enabled to reels and
reel_drafts where missing. Converts legacy reels.audience values (18_plus and
below_18) into age_restriction and defaults visibility audience to everyone.

Run with: python -m app.add_reel_audience_engagement_columns
"""
from sqlalchemy import inspect, text

COLUMNS = {
    "reels": {
        "age_restriction": "VARCHAR(20) NULL",
        "remixing_enabled": "BOOLEAN NOT NULL DEFAULT 1",
        "downloads_enabled": "BOOLEAN NOT NULL DEFAULT 1",
    },
    "reel_drafts": {
        "age_restriction": "VARCHAR(20) NULL",
        "remixing_enabled": "BOOLEAN NOT NULL DEFAULT 1",
        "downloads_enabled": "BOOLEAN NOT NULL DEFAULT 1",
    },
}


def migrate(engine) -> list[str]:
    changes: list[str] = []
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    # Keep DDL separate from the data backfill. This also works reliably with
    # SQLite's in-memory test databases, where inspecting through another
    # connection during an open transaction can interfere with that transaction.
    for table, columns in COLUMNS.items():
        if table not in tables:
            continue
        existing = {col["name"] for col in inspect(engine).get_columns(table)}
        for name, ddl in columns.items():
            if name in existing:
                continue
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
            existing.add(name)
            changes.append(f"{table}.{name} added")

    reel_cols = {col["name"] for col in inspect(engine).get_columns("reels")} if "reels" in tables else set()
    draft_cols = {col["name"] for col in inspect(engine).get_columns("reel_drafts")} if "reel_drafts" in tables else set()
    with engine.begin() as conn:
        if "reels" in tables:
            if {"audience", "age_restriction"}.issubset(reel_cols):
                result = conn.execute(text("UPDATE reels SET age_restriction = audience WHERE audience IN ('18_plus', 'below_18') AND age_restriction IS NULL"))
                if result.rowcount:
                    changes.append(f"reels legacy age labels migrated: {result.rowcount}")
                result = conn.execute(text("UPDATE reels SET audience = 'everyone' WHERE audience IS NULL OR audience IN ('18_plus', 'below_18')"))
                if result.rowcount:
                    changes.append(f"reels audience defaulted to everyone: {result.rowcount}")
        if "reel_drafts" in tables:
            if {"audience", "age_restriction"}.issubset(draft_cols):
                result = conn.execute(text("UPDATE reel_drafts SET age_restriction = audience WHERE audience IN ('18_plus', 'below_18') AND age_restriction IS NULL"))
                if result.rowcount:
                    changes.append(f"reel_drafts legacy age labels migrated: {result.rowcount}")
                result = conn.execute(text("UPDATE reel_drafts SET audience = 'everyone' WHERE audience IS NULL OR audience IN ('18_plus', 'below_18')"))
                if result.rowcount:
                    changes.append(f"reel_drafts audience defaulted to everyone: {result.rowcount}")
    return changes


def main() -> None:
    from app.database import engine
    changes = migrate(engine)
    print("\n".join(changes) if changes else "Nothing to do — already migrated.")
    print("Reel audience and engagement migration completed.")


if __name__ == "__main__":
    main()
