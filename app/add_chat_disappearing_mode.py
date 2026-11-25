"""Add the shared chat mode column.

Existing conversations default to disappearing mode, matching the new product
default. Run once against the target database:
    python -m app.add_chat_disappearing_mode
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
try:
    with conn.cursor() as cur:
        try:
            cur.execute(
                "ALTER TABLE conversation_disappearing_settings "
                "ADD COLUMN chat_mode VARCHAR(20) NOT NULL DEFAULT 'disappearing'"
            )
            print("conversation_disappearing_settings.chat_mode added")
        except pymysql.err.OperationalError as exc:
            # MySQL duplicate-column error means the migration was already run.
            if exc.args and exc.args[0] == 1060:
                print("conversation_disappearing_settings.chat_mode already exists")
            else:
                raise
    conn.commit()
finally:
    conn.close()
print("Done")
