import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATABASE_DIR = PROJECT_ROOT / "data"

DATABASE_DIR.mkdir(
    parents=True,
    exist_ok=True
)

DATABASE_PATH = DATABASE_DIR / "healthcare.db"

# HEALTHCARE_REFERRAL_DATABASE_URL overrides the default SQLite file. The test suite
# uses it to run against an isolated temporary database.
DATABASE_URL = os.environ.get(
    "HEALTHCARE_REFERRAL_DATABASE_URL",
    f"sqlite:///{DATABASE_PATH}",
)


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)
