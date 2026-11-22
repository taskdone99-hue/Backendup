"""Create missing long-form Video parity tables.

Run from the project root with the staging environment configured:
    venv/bin/python -m app.add_video_parity_tables

This migration is additive and uses checkfirst; it does not alter Reel tables.
"""
from app.database import engine
from app import models


def upgrade():
    tables = [
        models.VideoComment.__table__,
        models.VideoLike.__table__,
        models.VideoSavedItem.__table__,
        models.VideoTag.__table__,
        models.VideoInsightEvent.__table__,
        models.VideoAudioRemix.__table__,
        models.ContentInteractionEvent.__table__,
    ]
    for table in tables:
        table.create(bind=engine, checkfirst=True)


if __name__ == "__main__":
    upgrade()
    print("Video parity tables are ready.")
