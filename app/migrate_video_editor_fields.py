"""Add editor/location metadata columns to the long-form videos table.

Run explicitly against the intended database:
    python -m app.migrate_video_editor_fields

This migration is additive and safe to re-run. It does not drop or rewrite data.
"""
from sqlalchemy import inspect, text
from app.database import engine

COLUMNS = {
    "location_id": "INTEGER NULL",
    "location_name": "VARCHAR(150) NULL",
    "location_latitude": "FLOAT NULL",
    "location_longitude": "FLOAT NULL",
    "editor_state": "JSON NULL",
    "audio_id": "INTEGER NULL",
    "recording_duration_seconds": "FLOAT NULL",
    "timer_duration_seconds": "FLOAT NULL",
    "filter_id": "VARCHAR(50) NULL",
    "playback_speed": "FLOAT NULL",
    "audio_start_seconds": "FLOAT NULL",
    "tag_user_ids": "JSON NULL",
    "source": "VARCHAR(30) NULL",
}

def main():
    inspector = inspect(engine)
    if "videos" not in inspector.get_table_names():
        raise RuntimeError("videos table does not exist; verify the database before migration")
    existing = {column["name"] for column in inspector.get_columns("videos")}
    with engine.begin() as connection:
        for name, sql_type in COLUMNS.items():
            if name in existing:
                continue
            connection.execute(text(f"ALTER TABLE videos ADD COLUMN {name} {sql_type}"))
            print(f"Added videos.{name}")
    print("Video editor/location field migration complete.")

if __name__ == "__main__":
    main()
