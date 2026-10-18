"""Idempotently add optional watch-time split fields to creator collaboration requests.
Run with: python -m app.add_creator_collaboration_watch_time_columns
"""
import sys
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

TABLE = "creator_collaboration_requests"
COLUMNS = {
    "watch_time_share_percentage": "INTEGER NULL",
    # SQLAlchemy DateTime(timezone=True); MySQL stores this as DATETIME.
    "watch_time_expires_at": "DATETIME NULL",
}

def plan(engine: Engine) -> list[str]:
    inspector = inspect(engine)
    if TABLE not in inspector.get_table_names():
        return []
    existing = {c["name"] for c in inspector.get_columns(TABLE)}
    return [f"ALTER TABLE {TABLE} ADD COLUMN {name} {ddl}" for name, ddl in COLUMNS.items() if name not in existing]

def migrate(engine: Engine, dry_run: bool = False) -> list[str]:
    statements = plan(engine)
    if statements and not dry_run:
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))
    return statements

def main() -> None:
    from app.database import engine
    dry_run = "--dry-run" in sys.argv[1:]
    statements = migrate(engine, dry_run=dry_run)
    if not statements:
        print(f"{TABLE} is already up to date or does not exist yet")
    for statement in statements:
        print(("[dry-run] " if dry_run else "applied: ") + statement)
    print("Done")

if __name__ == "__main__":
    main()
