"""Add users.preferred_language. Safe to run repeatedly."""
from sqlalchemy import inspect, text
from app.database import engine

def migrate() -> None:
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        print("users table does not exist; nothing to migrate")
        return
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "preferred_language" in columns:
        print("users.preferred_language already exists")
        return
    dialect = engine.dialect.name
    if dialect == "mysql":
        statement = "ALTER TABLE users ADD COLUMN preferred_language VARCHAR(10) NOT NULL DEFAULT 'en'"
    else:
        statement = "ALTER TABLE users ADD COLUMN preferred_language VARCHAR(10) NOT NULL DEFAULT 'en'"
    with engine.begin() as connection:
        connection.execute(text(statement))
    print("Added users.preferred_language")

if __name__ == "__main__":
    migrate()
