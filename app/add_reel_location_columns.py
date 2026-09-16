"""
One-off migration for existing databases: adds the reel-location columns
to the pre-existing `reels` table (create_all() only creates brand-new
tables, it doesn't alter existing ones).

Usage:
    python -m app.add_reel_location_columns
"""

import pymysql

from app.database import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

conn = pymysql.connect(
    host=DB_HOST, port=int(DB_PORT), user=DB_USER, password=DB_PASSWORD, database=DB_NAME
)
cur = conn.cursor()

statements = [
    ("reels.location_name", "ALTER TABLE reels ADD COLUMN location_name VARCHAR(150) NULL"),
    ("reels.location_latitude", "ALTER TABLE reels ADD COLUMN location_latitude FLOAT NULL"),
    ("reels.location_longitude", "ALTER TABLE reels ADD COLUMN location_longitude FLOAT NULL"),
    (
        "reels.location_id",
        "ALTER TABLE reels ADD COLUMN location_id INT NULL, "
        "ADD CONSTRAINT fk_reels_location FOREIGN KEY (location_id) REFERENCES locations(id)",
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
