"""Idempotently add independent DM notification preferences.

Run against an existing MySQL database with:
    python -m app.add_conversation_notification_settings_columns
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
try:
    with conn.cursor() as cur:
        columns = [
            ("mute_messages", "ALTER TABLE conversation_mutes ADD COLUMN mute_messages BOOLEAN NOT NULL DEFAULT TRUE"),
            ("mute_calls", "ALTER TABLE conversation_mutes ADD COLUMN mute_calls BOOLEAN NOT NULL DEFAULT FALSE"),
            ("hide_message_previews", "ALTER TABLE conversation_mutes ADD COLUMN hide_message_previews BOOLEAN NOT NULL DEFAULT FALSE"),
        ]
        for name, sql in columns:
            try:
                cur.execute(sql)
                print(f"conversation_mutes.{name} added")
            except pymysql.err.OperationalError as exc:
                # MySQL 1060 = duplicate column; other errors should fail loudly.
                if exc.args and exc.args[0] == 1060:
                    print(f"conversation_mutes.{name} already exists")
                else:
                    raise
    conn.commit()
finally:
    conn.close()
print("Done")
