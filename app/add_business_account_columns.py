"""
One-off migration for existing databases: adds the columns behind the
business-account auto-intro DM feature.

    python -m app.add_business_account_columns
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

STATEMENTS = [
    (
        "users.account_type",
        "ALTER TABLE users ADD COLUMN account_type "
        "ENUM('personal','business') NOT NULL DEFAULT 'personal'",
    ),
    ("users.business_name", "ALTER TABLE users ADD COLUMN business_name VARCHAR(100) NULL"),
    (
        "users.business_category",
        "ALTER TABLE users ADD COLUMN business_category VARCHAR(100) NULL",
    ),
    (
        "users.business_description",
        "ALTER TABLE users ADD COLUMN business_description VARCHAR(500) NULL",
    ),
    (
        "messages.is_auto_message",
        "ALTER TABLE messages ADD COLUMN is_auto_message BOOLEAN NOT NULL DEFAULT FALSE",
    ),
]

for name, statement in STATEMENTS:
    try:
        cur.execute(statement)
        print(f"{name} added")
    except pymysql.err.OperationalError as e:
        print(f"{name}:", e)

conn.commit()
conn.close()
print("Done")
