"""
One-off migration for existing databases: adds the new chat-settings column
to `users`. New tables (highlights, highlight_items, shares, snaps,
conversations, conversation_participants, messages, notifications,
device_tokens) don't need this — Base.metadata.create_all() in main.py
creates them automatically on startup since they're new tables, not new
columns on an existing table.

Run once:
    python -m app.add_chat_settings_columns
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
    ("chat_font", "ALTER TABLE users ADD COLUMN chat_font VARCHAR(50) NULL"),
]

for name, statement in COLUMNS:
    try:
        cur.execute(statement)
        print(f"users.{name} added")
    except pymysql.err.OperationalError as e:
        print(f"users.{name}:", e)

conn.commit()
conn.close()
print("Done")
