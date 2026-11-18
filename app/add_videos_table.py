"""Create the independent long-form videos table.

Run with: python -m app.add_videos_table
This is additive and does not modify or migrate existing reels.
"""
from app.database import Base, engine
from app import models  # noqa: F401


def main() -> None:
    Base.metadata.create_all(bind=engine, tables=[models.Video.__table__])
    print("Created/verified videos table")


if __name__ == "__main__":
    main()
