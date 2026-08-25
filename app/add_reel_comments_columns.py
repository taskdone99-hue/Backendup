"""One-time MySQL migration for reel comments.

Run from the project root:
    python -m app.add_reel_comments_columns
"""
from sqlalchemy import text
from app.database import engine

def main():
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE comments MODIFY COLUMN post_id INT NULL"))
        try:
            conn.execute(text("ALTER TABLE comments ADD COLUMN reel_id INT NULL AFTER post_id"))
        except Exception as exc:
            if "Duplicate column" not in str(exc):
                raise
        try:
            conn.execute(text("CREATE INDEX ix_comments_reel_id ON comments (reel_id)"))
        except Exception as exc:
            if "Duplicate key name" not in str(exc):
                raise
        try:
            conn.execute(text(
                "ALTER TABLE comments ADD CONSTRAINT fk_comments_reel_id "
                "FOREIGN KEY (reel_id) REFERENCES reels(id)"
            ))
        except Exception as exc:
            if "already exists" not in str(exc).lower():
                raise
    print("Reel comments migration completed.")

if __name__ == "__main__":
    main()
