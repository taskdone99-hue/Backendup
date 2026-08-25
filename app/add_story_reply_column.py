"""
One-off migration for existing databases: adds the new reply_to_story_id
column to `messages`, used by the story-reply feature (POST
/api/stories/{story_id}/reply). The new `story_reactions` table doesn't need
this — Base.metadata.create_all() in main.py creates it automatically on
startup since it's a brand-new table, not a new column on an existing one.

Run once:
    python -m app.add_story_reply_column
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
    (
        "reply_to_story_id",
        "ALTER TABLE messages ADD COLUMN reply_to_story_id INT NULL, "
        "ADD INDEX ix_messages_reply_to_story_id (reply_to_story_id), "
        "ADD CONSTRAINT fk_messages_reply_to_story_id FOREIGN KEY (reply_to_story_id) "
        "REFERENCES stories(id) ON DELETE SET NULL",
    ),
]

for name, statement in COLUMNS:
    try:
        cur.execute(statement)
        print(f"messages.{name} added")
    except pymysql.err.OperationalError as e:
        print(f"messages.{name}:", e)

conn.commit()
conn.close()
print("Done")
