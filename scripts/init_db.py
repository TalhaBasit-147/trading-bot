"""Create all tables. Safe to run multiple times."""
from app.db.repo import init_db
from app.monitoring.logging_setup import setup_logging


if __name__ == "__main__":
    setup_logging()
    init_db()
    print("DB initialized.")
