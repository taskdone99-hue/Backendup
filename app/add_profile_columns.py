"""
One-off migration for existing databases: adds the new profile columns to
`users`. New tables (follows, posts, reels, saved_posts, stories,
story_views) don't need this — Base.metadata.create_all() in main.py
creates them automatically on startup since they're new tables, not new
columns on an existing table.

Run once:
    python -m app.add_profile_columns
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
    ("full_name", "ALTER TABLE users ADD COLUMN full_name VARCHAR(100) NULL"),
    ("bio", "ALTER TABLE users ADD COLUMN bio VARCHAR(150) NULL"),
    ("avatar_url", "ALTER TABLE users ADD COLUMN avatar_url VARCHAR(500) NULL"),
    (
        "is_private",
        "ALTER TABLE users ADD COLUMN is_private BOOLEAN NOT NULL DEFAULT FALSE",
    ),
]

for name, statement in COLUMNS:
    try:
        cur.execute(statement)
        print(f"users.{name} added")
    except pymysql.err.OperationalError as e:
        print(f"users.{name}:", e)

conn.commit()
conn.close()
print("Done")
