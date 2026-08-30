import pytest
from unittest.mock import MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models import Base, RecoveryCase, CaseStatus, CaseType, PaymentConfirmation
from app.main import process_payment_confirmation
from app.db.audit_repository import update_case_status

@pytest.fixture
def db():
    # Use SQLite memory DB for isolated, fast tests
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()

def test_update_case_status_rejects_recovered(db):
    case = RecoveryCase(
        id="case_status", case_type=CaseType.SUBSCRIPTION_FAILED, amount_paise=1000,
        currency="INR", customer_id="cust_1", status=CaseStatus.IN_PROGRESS, raw_signal_payload={}
    )
    db.add(case)
    db.commit()

    with pytest.raises(ValueError, match="recovered may only be reached by payment confirmation"):
        update_case_status(db, case.id, CaseStatus.RECOVERED)

    with pytest.raises(ValueError, match="partially_recovered may only be reached by payment confirmation"):
        update_case_status(db, case.id, CaseStatus.PARTIALLY_RECOVERED)

def test_partial_payment_and_overpayment(db):
    case = RecoveryCase(
        id="case_partial", case_type=CaseType.SUBSCRIPTION_FAILED, amount_paise=10000,
        currency="INR", customer_id="cust_1", status=CaseStatus.PAYMENT_PENDING, raw_signal_payload={}
    )
    db.add(case)
    db.commit()

    # Partial payment
    res1 = process_payment_confirmation(db, "pay_1", case.id, 4000, "webhook")
    assert res1["status"] == "partially_recovered"
    assert res1["recovered"] == 4000
    
    db.refresh(case)
    assert case.status == CaseStatus.PARTIALLY_RECOVERED

    # Completing the payment
    res2 = process_payment_confirmation(db, "pay_2", case.id, 6000, "webhook")
    assert res2["status"] == "recovered"
    assert res2["recovered"] == 10000

    db.refresh(case)
    assert case.status == CaseStatus.RECOVERED

    # Overpayment is escalated, but wait, the case is already RECOVERED and no longer PAYMENT_PENDING.
    # To test overpayment, we must test it while the case is STILL valid for payment.
    # Let's create a new case that gets overpaid in one shot.
    case2 = RecoveryCase(
        id="case_overpay", case_type=CaseType.SUBSCRIPTION_FAILED, amount_paise=1000,
        currency="INR", customer_id="cust_1", status=CaseStatus.PAYMENT_PENDING, raw_signal_payload={}
    )
    db.add(case2)
    db.commit()

    res3 = process_payment_confirmation(db, "pay_3", case2.id, 2000, "webhook")
    assert res3["status"] == "escalated"
    assert res3["reason"] == "overpayment"


import threading
import os
from sqlalchemy.exc import OperationalError

def test_postgresql_atomic_payment_concurrency():
    """
    Tests that two concurrent confirmations of the SAME payment_id 
    for the same case result in EXACTLY ONE insertion and ONE status update.
    """
    # Connect to local postgres used in docker-compose
    pg_url = os.getenv("DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/revenue_recovery")
    if "postgresql" not in pg_url:
        pytest.skip("PostgreSQL concurrency test requires a PostgreSQL DATABASE_URL")

    try:
        engine = create_engine(pg_url)
        with engine.connect() as conn:
            pass
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        db = SessionLocal()
    except Exception as e:
        pytest.skip(f"PostgreSQL not running or unreachable ({e}), skipping concurrency test")
    
    # Create test case
    import uuid
    case_id = f"case_conc_{uuid.uuid4().hex[:8]}"
    payment_id = f"pay_conc_{uuid.uuid4().hex[:8]}"
    
    case = RecoveryCase(
        id=case_id, case_type=CaseType.SUBSCRIPTION_FAILED, amount_paise=10000,
        currency="INR", customer_id="cust_1", status=CaseStatus.PAYMENT_PENDING, raw_signal_payload={}
    )
    db.add(case)
    db.commit()

    results = []
    
    def confirm_payment():
        session = SessionLocal()
        try:
            res = process_payment_confirmation(session, payment_id, case_id, 10000, "webhook")
            results.append(res["status"])
        except Exception as e:
            results.append(f"{e} - {type(e)}")
        finally:
            session.close()

    # Run two concurrent threads
    t1 = threading.Thread(target=confirm_payment)
    t2 = threading.Thread(target=confirm_payment)

    t1.start()
    t2.start()

    t1.join()
    t2.join()

    # Verify results: one should be "recovered", the other should be "idempotent"
    assert "recovered" in results
    assert "idempotent" in results

    # Verify DB state
    db.refresh(case)
    assert case.recovered_amount_paise == 10000
    assert case.status == CaseStatus.RECOVERED
    
    # Verify exact one payment confirmation record
    confirms = db.query(PaymentConfirmation).filter(PaymentConfirmation.payment_id == payment_id).all()
    assert len(confirms) == 1
    
    db.close()
