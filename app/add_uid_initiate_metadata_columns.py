"""Add UID-initiate metadata columns for Reel and Video.

Run once from the project root:
    ./venv/bin/python -m app.add_uid_initiate_metadata_columns

The migration is idempotent and only adds columns missing from existing tables.
"""
from sqlalchemy import inspect, text


COLUMNS = {
    "reels": {
        "hashtags": "JSON NULL",
    },
    "videos": {
        "age_restriction": "VARCHAR(20) NULL",
    },
}


def migrate(engine):
    changes = []
    for table, columns in COLUMNS.items():
        existing = {column["name"] for column in inspect(engine).get_columns(table)}
        for name, ddl in columns.items():
            if name in existing:
                continue
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
            changes.append(f"{table}.{name} added")
    return changes


def main():
    from app.database import engine

    changes = migrate(engine)
    for change in changes:
        print(change)
    print("UID initiate metadata migration completed." if changes else "Nothing to do — already migrated.")


if __name__ == "__main__":
    main()
