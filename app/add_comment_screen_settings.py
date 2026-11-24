"""Safely add the comment-screen settings schema to an existing database.

Run from the project root after taking a verified staging DB backup:
    python -m app.add_comment_screen_settings
"""
from sqlalchemy import inspect, text
from app import models  # noqa: F401 - registers models with Base metadata
from app.database import Base, engine


def main():
    inspector = inspect(engine)
    if not inspector.has_table("user_comment_settings"):
        raise RuntimeError("user_comment_settings table does not exist; refusing to guess the base schema")

    # Create only the new comment-block table, never all application metadata.
    Base.metadata.create_all(bind=engine, tables=[models.UserCommentBlock.__table__], checkfirst=True)

    inspector = inspect(engine)
    comment_columns = {column["name"] for column in inspector.get_columns("comments")}
    with engine.begin() as connection:
        if "media_url" not in comment_columns:
            connection.execute(text("ALTER TABLE comments ADD COLUMN media_url VARCHAR(1000) NULL"))
            print("Added comments.media_url")
        if "media_type" not in comment_columns:
            connection.execute(text("ALTER TABLE comments ADD COLUMN media_type VARCHAR(20) NULL"))
            print("Added comments.media_type")

    inspector = inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns("user_comment_settings")}
    additions = {
        "allow_story_comments_from": "ALTER TABLE user_comment_settings ADD COLUMN allow_story_comments_from VARCHAR(30) NOT NULL DEFAULT 'people_you_follow'",
        "allow_gif_comments": "ALTER TABLE user_comment_settings ADD COLUMN allow_gif_comments BOOLEAN NOT NULL DEFAULT TRUE",
        "allow_photo_comments": "ALTER TABLE user_comment_settings ADD COLUMN allow_photo_comments BOOLEAN NOT NULL DEFAULT TRUE",
    }
    with engine.begin() as connection:
        for name, statement in additions.items():
            if name not in columns:
                connection.execute(text(statement))
                print(f"Added user_comment_settings.{name}")
        # Expand only when inspection proves the existing field is narrower.
        current = columns.get("allow_comments_from")
        if current is None:
            raise RuntimeError("allow_comments_from column is missing; refusing to alter an unknown schema")
        type_text = str(current["type"]).lower()
        if "varchar(20)" in type_text or "varchar(30)" in type_text:
            connection.execute(text("ALTER TABLE user_comment_settings MODIFY COLUMN allow_comments_from VARCHAR(40) NOT NULL DEFAULT 'everyone'"))
            print("Expanded user_comment_settings.allow_comments_from to VARCHAR(40)")
    print("Comment screen settings migration completed.")


if __name__ == "__main__":
    main()
