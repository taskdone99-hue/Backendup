"""
One-off migration for existing databases: adds the new `location_id`
columns to `posts` and `stories`, for the Location feature. The new
`locations` table itself doesn't need this — Base.metadata.create_all() in
main.py creates it automatically on startup since it's a brand new table,
not a new column on an existing table.

Run once:
    python -m app.add_location_columns
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

COLUMNS = [
    (
        "posts.location_id",
        "ALTER TABLE posts ADD COLUMN location_id INT NULL, "
        "ADD INDEX ix_posts_location_id (location_id), "
        "ADD CONSTRAINT fk_posts_location FOREIGN KEY (location_id) REFERENCES locations(id)",
    ),
    (
        "stories.location_id",
        "ALTER TABLE stories ADD COLUMN location_id INT NULL, "
        "ADD INDEX ix_stories_location_id (location_id), "
        "ADD CONSTRAINT fk_stories_location FOREIGN KEY (location_id) REFERENCES locations(id)",
    ),
]

for name, statement in COLUMNS:
    try:
        cur.execute(statement)
        print(f"{name} added")
    except pymysql.err.OperationalError as e:
        print(f"{name}:", e)
    except pymysql.err.InternalError as e:
        print(f"{name}:", e)

conn.commit()
conn.close()
print("Done")
