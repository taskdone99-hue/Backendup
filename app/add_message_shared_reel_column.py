"""
One-off migration for existing databases: adds the `shared_reel_id` column
to `messages`, used by reel sharing in chat (POST
/api/chat/conversations/{id}/messages with `shared_reel_id`). Nullable and
additive — every existing message simply has no shared reel. Intentionally
not a foreign key (see models.Message.shared_reel_id).

Run once:
    python -m app.add_message_shared_reel_column
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
        "ALTER TABLE messages ADD COLUMN shared_reel_id INT NULL, "
        "ADD INDEX ix_messages_shared_reel_id (shared_reel_id)"
    )
    conn.commit()
    print("messages.shared_reel_id added")
except (pymysql.err.OperationalError, pymysql.err.InternalError) as e:
    print("messages.shared_reel_id:", e)

conn.close()
print("Done")
