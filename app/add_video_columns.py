"""
One-off migration for existing databases: adds the new `title` and
`remixed_from_id` columns to `reels` (needed for the /api/videos metadata
and /api/reels/:id/audio-remix endpoints).

New tables (comments, likes, reel_collaborators, reel_revenue_shares) don't
need this — Base.metadata.create_all() in main.py creates them
automatically on startup since they're new tables, not new columns on an
existing table.

Run once:
    python -m app.add_video_columns
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
    ("title", "ALTER TABLE reels ADD COLUMN title VARCHAR(150) NULL"),
    ("remixed_from_id", "ALTER TABLE reels ADD COLUMN remixed_from_id INT NULL"),
]

for name, statement in COLUMNS:
    try:
        cur.execute(statement)
        print(f"reels.{name} added")
    except pymysql.err.OperationalError as e:
        print(f"reels.{name}:", e)

try:
    cur.execute("ALTER TABLE reels ADD INDEX ix_reels_remixed_from_id (remixed_from_id)")
    print("reels.remixed_from_id index added")
except pymysql.err.OperationalError as e:
    print("reels.remixed_from_id index:", e)

conn.commit()
conn.close()
print("Done")
