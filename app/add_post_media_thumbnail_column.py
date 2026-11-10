"""Add thumbnail_url to post_media without changing existing media rows."""
from sqlalchemy import inspect, text
from app.database import engine

with engine.begin() as connection:
    columns = {column["name"] for column in inspect(connection).get_columns("post_media")}
    if "thumbnail_url" not in columns:
        connection.execute(text("ALTER TABLE post_media ADD COLUMN thumbnail_url VARCHAR(500) NULL"))
        print("Added post_media.thumbnail_url")
    else:
        print("post_media.thumbnail_url already exists")
