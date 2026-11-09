"""Create content-category preference and audit tables on an existing DB.

Run from the project root: python -m app.add_content_category_tables
Idempotent: existing tables are left untouched.
"""
from app.database import Base, engine
from app import models  # noqa: F401 - registers model metadata

TABLES = [
    models.UserContentCategory.__table__,
    models.ContentCategoryAudit.__table__,
]
Base.metadata.create_all(bind=engine, tables=TABLES, checkfirst=True)
print("Content category tables are ready.")
