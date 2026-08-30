from app import database
def SessionLocal():
    return database.SessionLocal()
from app import models
from app.domain import (
    RecoveryCaseContext, DiagnosisResult, RootCauseCategory,
    DecisionResult, RecoveryActionType, PolicyApprovedDecision,
    ExecutionStatus, ExecutionResult
)
from app.workflow import run_diagnosis_step, run_execution_step
from app.ai.provider import AIProvider
from typing import Type, Any
from sqlalchemy import select
import uuid
import sys

class FlakyAIProvider(AIProvider):
    def __init__(self, succeed: bool):
        self.succeed = succeed
        self.transaction_was_active = False

    def ask_structured(self, prompt: str, response_schema: Type[Any]) -> Any:
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
        
        # A. First checkpoint
        provider = FlakyAIProvider(succeed=True)
        # E. Transaction boundary (pre-check)
        # Refreshing db_case implicitly opens a transaction in SQLAlchemy. 
        # We explicitly close it here to prove the workflow step handles its own transactions.
        db.commit() 
        assert db.in_transaction() is False
        
        diagnosis = run_diagnosis_step(case_domain, provider)
        
        # Verify AI call didn't happen inside a transaction lock
        # run_diagnosis_step handles the transaction internally ONLY for the checkpoint.
        assert db.in_transaction() is False
        
        audit_logs = db.execute(select(models.AuditLog).where(models.AuditLog.case_id == case_domain.id)).scalars().all()
        assert len(audit_logs) == 1
        assert audit_logs[0].action_type == "DIAGNOSIS_COMPLETED"
        assert "soft_decline" in audit_logs[0].description
        
        # B. Replay: execute the exact same logical checkpoint twice
        # No exception should be raised, and length should still be 1.
        try:
            run_diagnosis_step(case_domain, provider)
        except Exception as e:
            assert False, f"Replay raised exception: {e}"
            
        audit_logs_after_replay = db.execute(select(models.AuditLog).where(models.AuditLog.case_id == case_domain.id)).scalars().all()
        assert len(audit_logs_after_replay) == 1 # Exactly one row exists
        
        # D. Logical attempt isolation
        # Simulate an intentional retry by advancing the retry_count (logical_attempt)
        case_domain.retry_count = 1
        run_diagnosis_step(case_domain, provider)
        
        audit_logs_after_new_attempt = db.execute(select(models.AuditLog).where(models.AuditLog.case_id == case_domain.id)).scalars().all()
        assert len(audit_logs_after_new_attempt) == 2 # Distinct checkpoint identity created!
        
        # C. Failure
        db_case_2 = get_db_case(db)
        case_domain_2 = RecoveryCaseContext.model_validate(db_case_2)
        
        provider_fail = FlakyAIProvider(succeed=False)
        try:
            run_diagnosis_step(case_domain_2, provider_fail)
            assert False, "Should have crashed"
        except Exception as e:
            assert "simulated crash" in str(e)
            
        audit_logs_fail = db.execute(select(models.AuditLog).where(models.AuditLog.case_id == case_domain_2.id)).scalars().all()
        assert len(audit_logs_fail) == 0 # No event written because AI failed
        
        print("SUCCESS: Workflow durable checkpoint tests passed.")
    finally:
        db.close()

class FlakyExecutor:
    def __init__(self, succeed: bool):
        self.succeed = succeed

    def execute(self, case: RecoveryCaseContext, approved_decision: PolicyApprovedDecision) -> ExecutionResult:
        if not self.succeed:
            raise Exception("Executor simulated crash during execution!")
        
        return ExecutionResult(
            status=ExecutionStatus.SUCCESS,
            action_taken=approved_decision.decision.recommended_action,
            reason="Mock execution succeeded",
            external_reference_id="mock_ref_123",
            action_parameters_used=approved_decision.decision.action_parameters
        )

def test_workflow_execution_retry_idempotency():
    db = SessionLocal()
    
    try:
        db_case = get_db_case(db)
        case_domain = RecoveryCaseContext.model_validate(db_case)
        
        approved_decision = PolicyApprovedDecision(
            decision=DecisionResult(
                recommended_action=RecoveryActionType.OFFER_DISCOUNT,
                action_parameters={"discount_percent": 10, "discount_applied_paise": 10000},
                confidence_score=0.9,
                reasoning="Test"
            ),
            policy_reason="Policy passed",
            idempotency_key="idem_exec_retry_123"
        )
        
        # A. Crash during execution step
        provider_fail = FlakyExecutor(succeed=False)
        try:
            run_execution_step(case_domain, approved_decision, provider_fail)
            assert False, "Should have crashed"
        except Exception as e:
            assert "simulated crash" in str(e)
            
        # Verify no execution logs written
        audit_logs_fail = db.execute(select(models.AuditLog).where(
            models.AuditLog.case_id == case_domain.id,
            models.AuditLog.action_type == "ACTION_EXECUTED"
        )).scalars().all()
        assert len(audit_logs_fail) == 0
        
        # B. Retry succeeds
        provider_succeed = FlakyExecutor(succeed=True)
        run_execution_step(case_domain, approved_decision, provider_succeed)
        
        audit_logs_success = db.execute(select(models.AuditLog).where(
            models.AuditLog.case_id == case_domain.id,
            models.AuditLog.action_type == "ACTION_EXECUTED"
        )).scalars().all()
        assert len(audit_logs_success) == 1
        
        # Refresh case and check discount applied
        db.commit()
        db.refresh(db_case)
        assert db_case.cumulative_discount_paise == 10000
        
        # C. Idempotent replay: exact same logical checkpoint again
        try:
            run_execution_step(case_domain, approved_decision, provider_succeed)
        except Exception as e:
            assert False, f"Replay raised exception: {e}"
            
        # Verify no duplicate logs
        audit_logs_replay = db.execute(select(models.AuditLog).where(
            models.AuditLog.case_id == case_domain.id,
            models.AuditLog.action_type == "ACTION_EXECUTED"
        )).scalars().all()
        assert len(audit_logs_replay) == 1

        # Check discount not double counted!
        db.commit()
        db.refresh(db_case)
        assert db_case.cumulative_discount_paise == 10000

        print("SUCCESS: Execution retry idempotency tests passed.")
    finally:
        db.close()

from app.db.audit_repository import update_case_status
from app.models import CaseStatus

def test_race_condition_guarding():
    db = SessionLocal()
    try:
        db_case = get_db_case(db)
        # Should succeed because it is OPEN
        updated = update_case_status(db, db_case.id, CaseStatus.IN_PROGRESS)
        assert updated is True
        
        # Now set it to a terminal state
        updated = update_case_status(db, db_case.id, CaseStatus.ESCALATED)
        assert updated is True
        
        # Now try to update it back to IN_PROGRESS (race condition)
        updated = update_case_status(db, db_case.id, CaseStatus.IN_PROGRESS)
        assert updated is False
        
        db.refresh(db_case)
        assert db_case.status == CaseStatus.ESCALATED
    finally:
        db.close()

if __name__ == "__main__":
    test_workflow_diagnosis()
    test_workflow_execution_retry_idempotency()
    test_race_condition_guarding()
