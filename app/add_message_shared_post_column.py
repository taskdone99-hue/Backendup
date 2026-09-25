"""
One-off migration for existing databases: adds the `shared_post_id` column
to `messages`, used by post sharing in chat (POST
/api/chat/conversations/{id}/messages with `shared_post_id`, and POST
/api/share/internal). Nullable and additive — every existing message simply
has no shared post. Intentionally not a foreign key (see
models.Message.shared_post_id) — same pattern as `shared_reel_id`, which
this sits alongside (see add_message_shared_reel_column.py).

Run once:
    python -m app.add_message_shared_post_column
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
        "ALTER TABLE messages ADD COLUMN shared_post_id INT NULL, "
        "ADD INDEX ix_messages_shared_post_id (shared_post_id)"
    )
    conn.commit()
    print("messages.shared_post_id added")
except (pymysql.err.OperationalError, pymysql.err.InternalError) as e:
    print("messages.shared_post_id:", e)

conn.close()
print("Done")
