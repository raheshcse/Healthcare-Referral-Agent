"""Schema initialization for the Healthcare Referral Agent."""
from sqlalchemy.engine import Engine
from backend.database.connection import engine as default_engine
from backend.database.models import Base
def ensure_schema(bind: Engine = default_engine) -> None:
    Base.metadata.create_all(bind)
