"""
One-off migration for existing databases: adds the `visibility` column to
`stories`, for the Close Friends feature (see models.StoryVisibility and
story_routes.py). Every pre-existing row backfills to 'public', which
preserves current behavior exactly. The new `close_friends` table itself
doesn't need this — Base.metadata.create_all() in main.py creates it
automatically on startup since it's a brand new table, not a new column on
an existing one (same for `story_drafts`).

Run once:
    python -m app.add_story_visibility_column
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

try:
    cur.execute(
        "ALTER TABLE stories ADD COLUMN visibility "
        "ENUM('public','close_friends') NOT NULL DEFAULT 'public'"
    )
    conn.commit()
    print("stories.visibility added")
except pymysql.err.OperationalError as e:
    print("stories.visibility:", e)

conn.close()
print("Done")
