from sqlalchemy.orm import declarative_base

Base = declarative_base()

from app.db import models  # noqa: E402,F401
