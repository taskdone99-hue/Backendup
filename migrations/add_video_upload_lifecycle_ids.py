"""Add stable public upload IDs and lifecycle fields to reels/videos.

Run from the project root: python migrations/add_video_upload_lifecycle_ids.py
Safe for SQLite and PostgreSQL; existing rows receive UUIDs and PUBLISHED state.
"""
import uuid
from sqlalchemy import inspect, text
from app.database import engine


def migrate():
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in ("reels", "videos"):
            if table not in inspector.get_table_names():
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            if "video_uid" not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN video_uid VARCHAR(36)"))
            if "upload_status" not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN upload_status VARCHAR(20) NOT NULL DEFAULT 'PUBLISHED'"))
            if "upload_progress" not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN upload_progress INTEGER NOT NULL DEFAULT 100"))
            rows = conn.execute(text(f"SELECT id FROM {table} WHERE video_uid IS NULL")).fetchall()
            for (row_id,) in rows:
                conn.execute(text(f"UPDATE {table} SET video_uid=:uid WHERE id=:id"), {"uid": str(uuid.uuid4()), "id": row_id})
        # Unique indexes are portable and protect against accidental duplicate IDs.
        for table in ("reels", "videos"):
            if table in inspector.get_table_names():
                conn.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS ix_{table}_video_uid ON {table} (video_uid)"))

if __name__ == "__main__":
    migrate()
    print("Upload lifecycle ID migration completed.")
