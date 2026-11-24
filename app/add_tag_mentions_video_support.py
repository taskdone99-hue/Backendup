"""Additive migration for tag approval on long videos and story-mention boost preference.

Run from the project root with the configured environment:
    venv/bin/python -m app.add_tag_mentions_video_support

Existing video tags remain approved and visible. Existing mention settings keep
the product default (story mention boosting enabled). Safe to rerun.
"""
import os
import pymysql
from dotenv import load_dotenv

load_dotenv()
conn = pymysql.connect(
    host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME"),
    port=int(os.getenv("DB_PORT", "3306")),
)
cur = conn.cursor()

STATEMENTS = [
    ("video_tags.is_approved", "ALTER TABLE video_tags ADD COLUMN is_approved TINYINT(1) NOT NULL DEFAULT 1"),
    ("video_tags.hidden_from_profile", "ALTER TABLE video_tags ADD COLUMN hidden_from_profile TINYINT(1) NOT NULL DEFAULT 0"),
    ("user_mention_settings.allow_story_mention_boost", "ALTER TABLE user_mention_settings ADD COLUMN allow_story_mention_boost TINYINT(1) NOT NULL DEFAULT 1"),
]
for label, sql in STATEMENTS:
    try:
        cur.execute(sql)
        conn.commit()
        print(f"{label}: added")
    except (pymysql.err.OperationalError, pymysql.err.InternalError) as exc:
        if "Duplicate column" in str(exc):
            print(f"{label}: already exists")
        else:
            raise
cur.close()
conn.close()
print("Tag, mention and video support migration complete.")
