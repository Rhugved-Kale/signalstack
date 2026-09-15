"""SQLAlchemy declarative base.

Kept in its own module so that both the models and Alembic's env.py can import
it without creating a circular import.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all SignalStack ORM models."""
