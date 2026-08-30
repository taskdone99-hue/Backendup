"""
One-off migration for existing databases: adds the edit/soft-delete
columns to the existing `messages` table.

The new `message_reactions` and `message_statuses` tables don't need this
script — they're brand-new tables, so Base.metadata.create_all() in
main.py creates them automatically the next time the app starts.

Run once:
    python -m app.add_message_edit_delete_columns
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
    ("messages.edited_at", "ALTER TABLE messages ADD COLUMN edited_at DATETIME NULL"),
    (
        "messages.is_deleted",
        "ALTER TABLE messages ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT FALSE",
    ),
    ("messages.deleted_at", "ALTER TABLE messages ADD COLUMN deleted_at DATETIME NULL"),
]

for name, statement in STATEMENTS:
    try:
        cur.execute(statement)
        print(f"{name} added")
    except pymysql.err.OperationalError as e:
        print(f"{name}:", e)

conn.commit()
conn.close()
print("Done")
