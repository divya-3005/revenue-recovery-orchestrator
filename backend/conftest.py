"""
Global test configuration.

Sets dummy API keys so provider constructors succeed in test environments.
Real API calls are always mocked at the ask_structured() level.
"""

import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.database import Base
import app.database

@pytest.fixture(autouse=True)
def set_test_env_vars(monkeypatch):
    """Ensure provider constructors don't fail due to missing API keys in tests."""
    monkeypatch.setenv("GEMINI_API_KEY", "test_dummy_key")
    monkeypatch.setenv("GROQ_API_KEY", "test_dummy_key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test_dummy_key")

@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    """Replace PostgreSQL with SQLite in memory for tests."""
    # Default to SQLite in-memory unless explicit TEST_DATABASE_URL or sqlite DATABASE_URL is set
    TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
    if not TEST_DATABASE_URL:
        db_env = os.getenv("DATABASE_URL", "")
        if "sqlite" in db_env:
            TEST_DATABASE_URL = db_env
        else:
            TEST_DATABASE_URL = "sqlite://"

    if "sqlite" in TEST_DATABASE_URL:
        engine = create_engine(
            TEST_DATABASE_URL, 
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        from sqlalchemy import event
        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.close()
    else:
        engine = create_engine(TEST_DATABASE_URL)

    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    
    # Patch the global SessionLocal and engine
    monkeypatch.setattr(app.database, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(app.database, "engine", engine)
    
    yield
    try:
        Base.metadata.drop_all(bind=engine)
    except Exception:
        pass
