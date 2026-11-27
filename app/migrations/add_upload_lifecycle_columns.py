"""Add upload lifecycle fields for Reel and long-form Video records.

Run once after deploying the code:
    python -m app.migrations.add_upload_lifecycle_columns
"""
from sqlalchemy import inspect, text
from app.database import engine

def main():
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in ("reels", "videos"):
            if table not in tables:
                continue
            columns = {c["name"] for c in inspect(conn).get_columns(table)}
            if "upload_status" not in columns:
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN upload_status VARCHAR(20) "
                    "NOT NULL DEFAULT 'PUBLISHED'"
                ))
            if "upload_session_id" not in columns:
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN upload_session_id VARCHAR(36)"
                ))
    print("Upload lifecycle columns are ready.")

if __name__ == "__main__":
    main()
