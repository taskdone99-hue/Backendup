"""
One-off migration for existing databases: adds the read-receipt watermark
column to `conversation_participants`. This table already exists on any
database that ran the earlier chat migration, so create_all() won't touch
it — create_all() only creates missing tables, never new columns on an
existing one.

Run once:
    python -m app.add_chat_realtime_columns
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
        "conversation_participants", "last_read_message_id",
        "ALTER TABLE conversation_participants ADD COLUMN last_read_message_id INT NULL",
    ),
]

for table, name, statement in COLUMNS:
    try:
        cur.execute(statement)
        print(f"{table}.{name} added")
    except pymysql.err.OperationalError as e:
        print(f"{table}.{name}:", e)

conn.commit()
conn.close()
print("Done")
