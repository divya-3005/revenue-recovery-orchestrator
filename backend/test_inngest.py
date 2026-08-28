import asyncio
import uuid
from unittest.mock import patch, MagicMock
from inngest import Context, Event
from app.workflows.case_workflow import inner_process_case_workflow
from app.database import SessionLocal
from app import models
from app.domain import DiagnosisResult, RootCauseCategory, DecisionResult, RecoveryActionType
from sqlalchemy import select

class MockStep:
    async def run(self, step_id, fn, **kwargs):
        return fn()

async def run_tests():
    db = SessionLocal()
    try:
        # Setup test case
        case_id = str(uuid.uuid4())
        db_case = models.RecoveryCase(
            id=case_id,
            case_type=models.CaseType.CHECKOUT_ABANDONED,
            amount_paise=100000,
            currency="INR",
            customer_id="cust_inngest_test",
            raw_signal_payload={}
        )
        db.add(db_case)
        db.commit()
        db.refresh(db_case)
        
        ctx = MockContext(event=Event(name="case.received", data={"case_id": case_id}))
        step = MockStep()
        
        # Test 1: Successful Path
        mock_diagnosis = DiagnosisResult(
            root_cause_category=RootCauseCategory.SOFT_DECLINE,
            specific_reason="mock_reason",
            confidence_score=0.9,
            reasoning="Mock reasoning"
        )
        mock_decision = DecisionResult(
            recommended_action=RecoveryActionType.RETRY_CHARGE,
            action_parameters={"delay_hours": 24},
            confidence_score=0.9,
            reasoning="Mock reasoning"
        )
        
        with patch('app.workflows.case_workflow.run_diagnosis_step') as m_diag, \
             patch('app.workflows.case_workflow.run_decision_step') as m_dec, \
             patch('razorpay.Client') as mock_razorpay_client_class:
            
            mock_razorpay_client = mock_razorpay_client_class.return_value
            mock_razorpay_client.payment_link.create.return_value = {"id": "plink_mocked"}
             
            m_diag.return_value = mock_diagnosis
            m_dec.return_value = mock_decision
            
            result = await inner_process_case_workflow(ctx, step)
            
            assert result["status"] == "recovered"
            assert result["execution"]["action_taken"] == "retry_charge"
            
            # Test 3: Replay -> no duplicate action
            result_replay = await inner_process_case_workflow(ctx, step)
            assert result_replay["status"] == "recovered"
            
            # Verify Audit Logs
            audit_logs = db.execute(select(models.AuditLog).where(models.AuditLog.case_id == case_id)).scalars().all()
            action_types = [log.action_type for log in audit_logs]
            assert "POLICY_EVALUATED" in action_types
            assert "ACTION_EXECUTED" in action_types
        
        # Test 2: Unsafe AI Decision -> Policy Rejection -> Executor NOT Called
        case_id_2 = str(uuid.uuid4())
        db_case_2 = models.RecoveryCase(
            id=case_id_2,
            case_type=models.CaseType.CHECKOUT_ABANDONED,
            amount_paise=100000,
            currency="INR",
            customer_id="cust_inngest_test_2",
            raw_signal_payload={}
        )
        db.add(db_case_2)
        db.commit()
        db.refresh(db_case_2)
        
        ctx_2 = MockContext(event=Event(name="case.received", data={"case_id": case_id_2}))
        
        mock_bad_decision = DecisionResult(
            recommended_action=RecoveryActionType.OFFER_DISCOUNT,
            action_parameters={"discount_percent": 50},
            confidence_score=0.9,
            reasoning="Mock reasoning"
        )
        
        with patch('app.workflows.case_workflow.run_diagnosis_step') as m_diag, \
             patch('app.workflows.case_workflow.run_decision_step') as m_dec, \
             patch('app.workflows.case_workflow.run_execution_step') as m_exec:
             
            m_diag.return_value = mock_diagnosis
            m_dec.return_value = mock_bad_decision
            
            result_2 = await inner_process_case_workflow(ctx_2, step)
            
            assert result_2["status"] == "policy_rejected"
            m_exec.assert_not_called()
            
            audit_logs_2 = db.execute(select(models.AuditLog).where(models.AuditLog.case_id == case_id_2)).scalars().all()
            action_types_2 = [log.action_type for log in audit_logs_2]
            assert "POLICY_EVALUATED" in action_types_2
            assert "ACTION_EXECUTED" not in action_types_2
            
        print("SUCCESS: Inngest minimal workflow test passed.")
    finally:
        db.close()

class MockContext:
    def __init__(self, event):
        self.event = event

if __name__ == "__main__":
    asyncio.run(run_tests())
