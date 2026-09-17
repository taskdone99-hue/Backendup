"""
One-off migration for existing databases: adds the per-photo `caption`
column to the pre-existing `post_media` table (create_all() only creates
brand-new tables, it doesn't alter existing ones).

Usage:
    python -m app.add_post_media_caption_column
"""

import pymysql

from app.database import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

conn = pymysql.connect(
    host=DB_HOST, port=int(DB_PORT), user=DB_USER, password=DB_PASSWORD, database=DB_NAME
)
cur = conn.cursor()

try:
    cur.execute("ALTER TABLE post_media ADD COLUMN caption VARCHAR(2200) NULL")
    print("post_media.caption: added")
except pymysql.err.OperationalError as e:
    print("post_media.caption:", e)

conn.commit()
conn.close()
print("Done")
