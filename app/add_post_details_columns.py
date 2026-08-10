"""
One-off migration for existing databases: adds the Add Music / Add Location
columns to `posts`. The two new tables this feature needs (post_tags,
post_members) don't need this — Base.metadata.create_all() in main.py
creates them automatically on startup since they're new tables, not new
columns on an existing one.

Run once:
    python -m app.add_post_details_columns
"""

import os

import pymysql
from dotenv import load_dotenv

load_dotenv()

conn = pymysql.connect(
    host=os.getenv("DB_HOST"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    database=os.getenv("DB_NAME"),
    port=int(os.getenv("DB_PORT", "3306")),
)
cur = conn.cursor()

COLUMNS = [
    ("music_title", "ALTER TABLE posts ADD COLUMN music_title VARCHAR(150) NULL"),
    ("music_artist", "ALTER TABLE posts ADD COLUMN music_artist VARCHAR(150) NULL"),
    ("music_url", "ALTER TABLE posts ADD COLUMN music_url VARCHAR(500) NULL"),
    ("music_start_seconds", "ALTER TABLE posts ADD COLUMN music_start_seconds INT NULL"),
    ("location_name", "ALTER TABLE posts ADD COLUMN location_name VARCHAR(150) NULL"),
    ("location_latitude", "ALTER TABLE posts ADD COLUMN location_latitude FLOAT NULL"),
    ("location_longitude", "ALTER TABLE posts ADD COLUMN location_longitude FLOAT NULL"),
]

for name, statement in COLUMNS:
    try:
        cur.execute(statement)
        print(f"posts.{name} added")
    except pymysql.err.OperationalError as e:
        print(f"posts.{name}:", e)

conn.commit()
conn.close()
print("Done")
