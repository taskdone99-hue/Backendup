"""
One-off migration for existing databases, covering two changes:

1. Adds `alt_text` and `ai_generated` to the existing `posts` table (used by
   the expanded PUT /api/posts/:id).
2. Copies every row from the legacy `saved_posts` table into the new
   `saved_items` table (target_type='post'), so previously-saved posts still
   show up under the new Saved API. `saved_posts` itself is left in place,
   untouched, in case anything else still reads it.

Brand-new tables (series, series_reels, audio_tracks, saved_collections,
saved_items, saved_collection_items) don't need this script —
Base.metadata.create_all() in main.py creates them automatically on
startup since they're new tables, not new columns on an existing one. Just
make sure the app has started at least once (creating saved_items) BEFORE
running this script, since step 2 inserts into it.

Run once, after the app has started at least once with the new models:
    python -m app.add_saved_and_post_edit_columns
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

# ---- 1. New Post columns ----

COLUMNS = [
    ("alt_text", "ALTER TABLE posts ADD COLUMN alt_text VARCHAR(1000) NULL"),
    (
        "ai_generated",
        "ALTER TABLE posts ADD COLUMN ai_generated BOOLEAN NOT NULL DEFAULT FALSE",
    ),
]

for name, statement in COLUMNS:
    try:
        cur.execute(statement)
        print(f"posts.{name} added")
    except pymysql.err.OperationalError as e:
        print(f"posts.{name}:", e)

conn.commit()

# ---- 2. Copy saved_posts -> saved_items ----

try:
    cur.execute(
        """
        INSERT IGNORE INTO saved_items (user_id, target_type, target_id, created_at)
        SELECT user_id, 'post', post_id, created_at FROM saved_posts
        """
    )
    conn.commit()
    print(f"Copied {cur.rowcount} saved_posts rows into saved_items")
except pymysql.err.ProgrammingError as e:
    print(
        "Couldn't copy saved_posts -> saved_items (has the app started at least once "
        "since the update, to create saved_items?):",
        e,
    )

conn.close()
print("Done")
