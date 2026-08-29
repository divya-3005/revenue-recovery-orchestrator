"""
Tests that PostgreSQL enum values match the Python model definitions.
Catches schema drift between Alembic migrations and Python enums.
"""

from app.database import engine
from sqlalchemy import text
from app.models import CaseStatus


def test_casestatus_enum_values_match_db():
    """Verify the DB enum has all the values the Python CaseStatus enum expects."""
    with engine.connect() as conn:
        res = conn.execute(text(
            "SELECT enumlabel FROM pg_enum "
            "JOIN pg_type ON pg_enum.enumtypid = pg_type.oid "
            "WHERE pg_type.typname = 'casestatus';"
        ))
        db_values = {r[0] for r in res}

    python_values = {s.name for s in CaseStatus}

    assert python_values.issubset(db_values), (
        f"Python CaseStatus has values not in DB: {python_values - db_values}"
    )
