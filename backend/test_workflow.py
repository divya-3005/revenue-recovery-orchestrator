from app.database import SessionLocal
from app import models
from app.domain import RecoveryCaseContext, DiagnosisResult, RootCauseCategory
from app.workflow import run_diagnosis_step
from app.ai.provider import AIProvider
from typing import Type, Any
from sqlalchemy import select
import uuid
import sys

class FlakyAIProvider(AIProvider):
    def __init__(self, succeed: bool):
        self.succeed = succeed
        self.transaction_active_during_call = None

    def ask_structured(self, prompt: str, response_schema: Type[Any]) -> Any:
        # In a real scenario, holding a DB lock during a 5 second network call is terrible.
        # We simulate checking if a transaction is currently holding locks.
        # SQLAlchemy Session.is_active is True even before a transaction begins in 2.0, 
        # but we can just check if we are explicitly in a transaction if we needed to.
        # For this test, we'll just trust that `run_diagnosis_step` doesn't do `with session.begin()` spanning this.
        if not self.succeed:
            raise Exception("AI Provider simulated crash during diagnosis!")
            
        return DiagnosisResult(
            root_cause_category=RootCauseCategory.SOFT_DECLINE,
            specific_reason="insufficient_funds",
            confidence_score=0.9,
            reasoning="Test reasoning"
        )

def get_db_case(db) -> models.RecoveryCase:
    db_case = models.RecoveryCase(
        id=str(uuid.uuid4()),
        case_type=models.CaseType.CHECKOUT_ABANDONED,
        amount_paise=100000,
        currency="INR",
        customer_id="cust_workflow_test",
        raw_signal_payload={}
    )
    db.add(db_case)
    db.commit()
    db.refresh(db_case)
    return db_case

def test_workflow_diagnosis():
    db = SessionLocal()
    
    try:
        db_case = get_db_case(db)
        case_domain = RecoveryCaseContext.model_validate(db_case)
        
        # 1. Diagnosis Succeeds -> Checkpoint Exists
        provider = FlakyAIProvider(succeed=True)
        diagnosis = run_diagnosis_step(db, case_domain, provider)
        
        assert diagnosis.root_cause_category == RootCauseCategory.SOFT_DECLINE
        
        # Verify AuditLog was written
        audit_logs = db.execute(select(models.AuditLog).where(models.AuditLog.case_id == case_domain.id)).scalars().all()
        assert len(audit_logs) == 1
        assert audit_logs[0].action_type == "DIAGNOSIS_COMPLETED"
        assert "soft_decline" in audit_logs[0].description
        
        # 3. Replaying the same checkpoint does not create duplicate events
        # We explicitly run the step again with the SAME state (retry_count=0)
        run_diagnosis_step(db, case_domain, provider)
        
        audit_logs_after_replay = db.execute(select(models.AuditLog).where(models.AuditLog.case_id == case_domain.id)).scalars().all()
        assert len(audit_logs_after_replay) == 1 # Still exactly 1! Idempotency worked.
        
        # 2. Diagnosis Fails -> No event written
        db_case_2 = get_db_case(db)
        case_domain_2 = RecoveryCaseContext.model_validate(db_case_2)
        
        provider_fail = FlakyAIProvider(succeed=False)
        try:
            run_diagnosis_step(db, case_domain_2, provider_fail)
            assert False, "Should have crashed"
        except Exception as e:
            assert "simulated crash" in str(e)
            
        audit_logs_fail = db.execute(select(models.AuditLog).where(models.AuditLog.case_id == case_domain_2.id)).scalars().all()
        assert len(audit_logs_fail) == 0 # No event written because AI failed before checkpoint
        
        print("SUCCESS: Workflow durable checkpoint tests passed.")
    finally:
        db.close()

if __name__ == "__main__":
    test_workflow_diagnosis()
