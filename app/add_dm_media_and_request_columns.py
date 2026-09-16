"""
One-off migration for existing databases: adds the columns the new
Advanced-DM feature needs on two tables that already existed
(`messages`, `conversation_participants`) — create_all() only creates
brand-new tables, it doesn't alter existing ones (see the comment above
Base.metadata.create_all(bind=engine) in app/main.py).

Uses the same DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD env vars (from
.env) as the rest of the app, via app.database — no separate credentials
to manage or accidentally commit.

Safe to run more than once: every statement is wrapped so an
"already exists" error is reported and skipped rather than raised.

Usage:
    python -m app.add_dm_media_and_request_columns
"""

import pymysql

from app.database import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

conn = pymysql.connect(
    host=DB_HOST, port=int(DB_PORT), user=DB_USER, password=DB_PASSWORD, database=DB_NAME
)
cur = conn.cursor()

statements = [
    ("messages.content nullable", "ALTER TABLE messages MODIFY COLUMN content VARCHAR(2200) NULL"),
    ("messages.media_url", "ALTER TABLE messages ADD COLUMN media_url VARCHAR(500) NULL"),
    ("messages.media_type", "ALTER TABLE messages ADD COLUMN media_type ENUM('image','video','audio') NULL"),
    (
        "messages.reply_to_message_id",
        "ALTER TABLE messages ADD COLUMN reply_to_message_id INT NULL, "
        "ADD CONSTRAINT fk_messages_reply_to FOREIGN KEY (reply_to_message_id) "
        "REFERENCES messages(id) ON DELETE SET NULL",
    ),
    (
        "conversation_participants.status",
        "ALTER TABLE conversation_participants ADD COLUMN status "
        "ENUM('accepted','pending') NOT NULL DEFAULT 'accepted'",
    ),
]

for label, sql in statements:
    try:
        cur.execute(sql)
        print(f"{label}: added")
    except pymysql.err.OperationalError as e:
        print(f"{label}: {e}")

conn.commit()
conn.close()
print("Done")
