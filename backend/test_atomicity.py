import sys
from app.database import SessionLocal
from app import models
from sqlalchemy import select

def test_atomicity():
    db = SessionLocal()
    customer_id = "cust_fail_test_123"
    
    print(f"Starting atomic transaction test for customer {customer_id}...")
    try:
        # 1. Prepare Case
        db_case = models.RecoveryCase(
            case_type=models.CaseType.CHECKOUT_ABANDONED,
            amount_paise=99900,
            currency="INR",
            customer_id=customer_id,
            raw_signal_payload={"event": "test"}
        )
        
        # 2. Prepare Audit Log
        audit_log = models.AuditLog(
            action_type="SIGNAL_RECEIVED",
            description="Test case creation before crash",
            reasoning="Testing atomicity"
        )
        db_case.audit_logs.append(audit_log)
        
        db.add(db_case)
        # Flush pushes the SQL to the database in the current transaction, but does NOT commit it.
        db.flush() 
        print("Flushed to DB. Simulating a crash before commit...")
        
        # 3. Simulate a critical crash BEFORE the commit
        raise Exception("Simulated critical failure during transaction!")
        
        db.commit()
    except Exception as e:
        print(f"Caught exception: {e}")
        # Rollback is called (which FastAPI Depends(get_db) would do automatically on error)
        db.rollback()
        print("Rolled back transaction.")
        
    # 4. Verify nothing was persisted
    case_query = db.execute(
        select(models.RecoveryCase).where(models.RecoveryCase.customer_id == customer_id)
    ).scalars().first()
    
    audit_query = db.execute(
        select(models.AuditLog).where(models.AuditLog.description == "Test case creation before crash")
    ).scalars().first()
    
    if case_query is not None:
        print("FAIL: RecoveryCase was persisted despite the failure!")
        sys.exit(1)
        
    if audit_query is not None:
        print("FAIL: AuditLog was persisted despite the failure!")
        sys.exit(1)
        
    print("SUCCESS: Atomicity verified. Neither the Case nor the AuditLog remains in the database.")
    db.close()

if __name__ == "__main__":
    test_atomicity()
