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
    # Ensure we use an in-memory SQLite database for tests
    # We use shared cache so that multiple threads (e.g. TestClient and webhook workers)
    # can share the same in-memory database instance.
    TEST_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///file:testdb?mode=memory&cache=shared&uri=true")

    # We only override the database configuration if it's the test DB
    if "sqlite" in TEST_DATABASE_URL:
        engine = create_engine(
            "sqlite://", 
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
