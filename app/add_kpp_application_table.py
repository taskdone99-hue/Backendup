"""Create KPP application storage. Safe to run repeatedly."""
from app.database import engine
from app.models import KPPApplication


def main() -> None:
    KPPApplication.__table__.create(bind=engine, checkfirst=True)
    print("kpp_applications table is present")


if __name__ == "__main__":
    main()
