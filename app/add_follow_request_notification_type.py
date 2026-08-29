"""
One-off migration for existing databases: adds the new 'follow_request'
value to the notifications.type ENUM column.

The new `follow_requests` table doesn't need this script — it's a
brand-new table, so Base.metadata.create_all() in main.py creates it
automatically the next time the app starts.

Run once:
    python -m app.add_follow_request_notification_type
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
        "ALTER TABLE notifications MODIFY COLUMN type "
        "ENUM('like','comment','follow','follow_request','mention','share','message','other') "
        "NOT NULL DEFAULT 'other'"
    )
    conn.commit()
    print("notifications.type now accepts 'follow_request'")
except pymysql.err.OperationalError as e:
    print("notifications.type:", e)

conn.close()
print("Done")
