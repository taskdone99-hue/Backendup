
"""Create messaging settings and audit tables on an existing database."""

from app.database import Base, engine
from app import models  # noqa: F401 - register model metadata

TABLES = [
    models.UserMessageSettings.__table__,
    models.MessageSettingsAudit.__table__,
]

Base.metadata.create_all(
    bind=engine,
    tables=TABLES,
    checkfirst=True,
)

print("Message settings tables are ready.")
