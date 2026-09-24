"""
One-off migration for existing databases, for the user-tag features
(tag approval / hide-from-profile / tag notifications — see
app/services/tag_service.py and app/routers/tag_routes.py):

  1. post_tags / reel_tags: adds `is_approved` (default 1) and
     `hidden_from_profile` (default 0). Every existing tag stays approved and
     visible.
  2. notifications.type: adds the 'tag' and 'tag_request' values to the ENUM.
     Without this MySQL rejects the INSERT the first time someone is tagged.
     (The list below is every value in models.NotificationType.)

The new `story_tags` and `user_tag_settings` tables need no script —
Base.metadata.create_all() in main.py creates them on startup.

Run once:
    python -m app.add_user_tag_features
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

STATEMENTS = [
    (
        "post_tags flags",
        "ALTER TABLE post_tags "
        "ADD COLUMN is_approved TINYINT(1) NOT NULL DEFAULT 1, "
        "ADD COLUMN hidden_from_profile TINYINT(1) NOT NULL DEFAULT 0",
    ),
    (
        "reel_tags flags",
        "ALTER TABLE reel_tags "
        "ADD COLUMN is_approved TINYINT(1) NOT NULL DEFAULT 1, "
        "ADD COLUMN hidden_from_profile TINYINT(1) NOT NULL DEFAULT 0",
    ),
    (
        "notifications.type enum",
        "ALTER TABLE notifications MODIFY COLUMN type "
        "ENUM('like','comment','follow','follow_request','mention','share',"
        "'message','collaboration_request','collaboration_accepted',"
        "'collaboration_rejected','collaboration_cancelled','moderation_warning',"
        "'brand_collaboration_offer','brand_collaboration_accepted',"
        "'brand_collaboration_rejected','tag','tag_request','other') "
        "NOT NULL DEFAULT 'other'",
    ),
]

for label, statement in STATEMENTS:
    try:
        cur.execute(statement)
        conn.commit()
        print(f"{label}: done")
    except (pymysql.err.OperationalError, pymysql.err.InternalError) as e:
        # e.g. "Duplicate column name" when re-run — safe to ignore.
        print(f"{label}:", e)

conn.close()
print("Done")
