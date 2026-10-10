"""Create the additive tables for reel captions, drafts, and comment-DM settings.

Safe to run repeatedly. Existing reels/posts/users are not altered.
Run from the project root with: python -m app.add_video_upload_pending_features
"""
from app import models
from app.database import engine


def migrate() -> None:
    models.Base.metadata.create_all(
        bind=engine,
        tables=[
            models.ReelCaptionTrack.__table__,
            models.ReelDraft.__table__,
            models.ReelCommentDMAutomation.__table__,
        ],
    )


if __name__ == "__main__":
    migrate()
    print("Video upload pending-feature tables created (or already existed).")
