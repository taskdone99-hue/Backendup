"""Additive migration for editor/recording metadata on long-form videos.

Run from the project root: python -m app.add_long_video_editor_fields
Back up the database before running. Safe to rerun.
"""
from sqlalchemy import inspect, text
from app.database import engine
from app import models  # noqa: F401

columns = {column["name"] for column in inspect(engine).get_columns("videos")}
new_columns = {
    "location_name": "VARCHAR(150) NULL",
    "location_id": "INT NULL",
    "location_latitude": "FLOAT NULL",
    "location_longitude": "FLOAT NULL",
    "editor_state": "JSON NULL",
    "audio_id": "INT NULL",
    "recording_duration_seconds": "FLOAT NULL",
    "timer_duration_seconds": "FLOAT NULL",
    "filter_id": "VARCHAR(50) NULL",
    "playback_speed": "FLOAT NULL",
    "audio_start_seconds": "FLOAT NULL",
    "tag_user_ids": "JSON NULL",
    "source": "VARCHAR(30) NULL",
}
with engine.begin() as connection:
    for name, sql_type in new_columns.items():
        if name not in columns:
            connection.execute(text(f"ALTER TABLE videos ADD COLUMN {name} {sql_type}"))
            print(f"videos.{name} added")
print("Long-video editor metadata migration complete.")
