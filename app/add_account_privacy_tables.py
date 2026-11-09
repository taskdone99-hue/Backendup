"""Create account privacy preference and audit tables on an existing DB.

Run from the project root: python -m app.add_account_privacy_tables
Idempotent: existing tables are left untouched.
"""
from app.database import Base, engine
from app import models  # noqa: F401 - registers the model metadata

TABLES = [
    models.UserPrivacySettings.__table__,
    models.PrivacySettingsAudit.__table__,
]
Base.metadata.create_all(bind=engine, tables=TABLES, checkfirst=True)
print("Account privacy tables are ready.")
