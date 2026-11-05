"""Expand otp_codes.purpose for authenticated account-management flows.

Run on an existing MySQL/MariaDB database before deploying the updated
models/routes:

    python -m app.add_account_management_otp_purposes

The migration is additive and safe to rerun. SQLite test databases create the
updated enum schema directly from app.models and need no ALTER statement.
"""

from sqlalchemy import inspect, text

from app.database import engine


PURPOSE_ENUM = (
    "ENUM('signup','login','reset_password','change_phone',"
    "'change_email','delete_account') NOT NULL"
)


def main() -> None:
    inspector = inspect(engine)
    if not inspector.has_table("otp_codes"):
        raise RuntimeError("otp_codes table does not exist; run the app's base schema setup first")

    if engine.dialect.name in ("mysql", "mariadb"):
        with engine.begin() as connection:
            connection.execute(
                text(f"ALTER TABLE otp_codes MODIFY COLUMN purpose {PURPOSE_ENUM}")
            )
        print("otp_codes.purpose now supports change_phone, change_email and delete_account")
    elif engine.dialect.name == "sqlite":
        print("SQLite stores the updated OTP purpose values as strings; no migration needed")
    else:
        raise RuntimeError(
            f"Unsupported database dialect for this migration: {engine.dialect.name}"
        )

    print("Done")


if __name__ == "__main__":
    main()
