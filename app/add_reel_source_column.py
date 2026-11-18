"""Add source metadata to existing reels.

Run with: python -m app.add_reel_source_column
Existing reels are marked as gallery uploads; new uploads can specify gallery or camera.
"""
from sqlalchemy import inspect, text
from app.database import engine


def main() -> None:
    inspector = inspect(engine)
    if "reels" not in inspector.get_table_names():
        raise RuntimeError("reels table does not exist; deploy the application schema first")
    columns = {column["name"] for column in inspector.get_columns("reels")}
    if "source" not in columns:
        with engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE reels ADD COLUMN source VARCHAR(20) NOT NULL DEFAULT 'gallery'"
            ))
        print("Added reels.source")
    else:
        print("reels.source already exists")


if __name__ == "__main__":
    main()
