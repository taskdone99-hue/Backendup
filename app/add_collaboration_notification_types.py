"""
One-off migration for existing databases: adds the three collaboration
values to the notifications.type ENUM column —
'collaboration_request', 'collaboration_accepted' and
'collaboration_rejected' (see app/services/collaboration_service.py).

Without this, MySQL rejects the INSERT the moment someone sends a
collaboration request, because the column's ENUM is still the old list.

The creator_collaboration_requests table itself doesn't need this script —
Base.metadata.create_all() in main.py already creates it.

Run once:
    python -m app.add_collaboration_notification_types
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
        "ENUM('like','comment','follow','follow_request','mention','share',"
        "'message','collaboration_request','collaboration_accepted',"
        "'collaboration_rejected','other') "
        "NOT NULL DEFAULT 'other'"
    )
    conn.commit()
    print("notifications.type now accepts the collaboration_* values")
except pymysql.err.OperationalError as e:
    print("notifications.type:", e)

conn.close()
print("Done")
