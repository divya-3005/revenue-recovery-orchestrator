"""
Test configuration — in-memory SQLite for all tests.
"""

import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.database as db_module
from app.database import Base


@pytest.fixture(autouse=True)
def test_db(monkeypatch):
    """Replace the real database with in-memory SQLite for every test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Import models so Base knows about them, then create tables
    import app.models  # noqa: F401
    Base.metadata.create_all(bind=engine)

    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", TestSession)

    yield engine

    Base.metadata.drop_all(bind=engine)
