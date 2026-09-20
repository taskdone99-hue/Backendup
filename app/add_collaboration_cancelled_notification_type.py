"""
One-off migration for existing databases: adds 'collaboration_cancelled'
to the notifications.type ENUM column (see
app/services/collaboration_service.py's cancel_request, which now sends
this notification to the invited partner when the requester cancels).

Without this, MySQL rejects the INSERT the moment someone cancels a
collaboration request, because the column's ENUM is still the old list —
same reason app/add_collaboration_notification_types.py exists for the
original three collaboration_* values.

Run once:
    python -m app.add_collaboration_cancelled_notification_type
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
        "'collaboration_rejected','collaboration_cancelled',"
        "'brand_collaboration_offer','brand_collaboration_accepted',"
        "'brand_collaboration_rejected','other') "
        "NOT NULL DEFAULT 'other'"
    )
    conn.commit()
    print("notifications.type now accepts 'collaboration_cancelled'")
except pymysql.err.OperationalError as e:
    print("notifications.type:", e)

conn.close()
print("Done")
