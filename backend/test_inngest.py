"""
Tests for Inngest workflow — verifies the recovery pipeline's core loop.

Tests:
  1. Successful path: soft decline → retry → recovered
  2. Unsafe AI decision → policy rejection → executor NOT called
"""

import asyncio
import uuid
import pytest
from unittest.mock import patch, MagicMock
from inngest import Context, Event
from app.workflows.case_workflow import inner_process_case_workflow
from app.database import SessionLocal
from app import models
from app.domain import DiagnosisResult, RootCauseCategory, DecisionResult, RecoveryActionType
from sqlalchemy import select


class MockStep:
    """Simulates Inngest step.run — calls function synchronously, no memoization."""
    def __init__(self, case_id=None):
        self.case_id = case_id

    async def run(self, step_id, fn, **kwargs):
        return fn()

    async def sleep(self, step_id, duration):
        # Simulate a webhook confirming payment during the sleep window
        if self.case_id:
            from app.db.audit_repository import update_case_status
            db = SessionLocal()
            try:
                update_case_status(db, self.case_id, models.CaseStatus.RECOVERED)
            finally:
                db.close()


class MockContext:
    def __init__(self, event):
        self.event = event


@pytest.mark.asyncio
async def test_inngest_successful_path():
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
        step = MockStep(case_id=case_id)

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

            # Verify Audit Logs
            audit_logs = db.execute(select(models.AuditLog).where(models.AuditLog.case_id == case_id)).scalars().all()
            action_types = [log.action_type for log in audit_logs]
            assert "POLICY_EVALUATED" in action_types
            assert "ACTION_EXECUTED" in action_types
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inngest_policy_rejection():
    db = SessionLocal()
    try:
        case_id = str(uuid.uuid4())
        db_case = models.RecoveryCase(
            id=case_id,
            case_type=models.CaseType.CHECKOUT_ABANDONED,
            amount_paise=100000,
            currency="INR",
            customer_id="cust_inngest_test_2",
            raw_signal_payload={}
        )
        db.add(db_case)
        db.commit()
        db.refresh(db_case)

        ctx = MockContext(event=Event(name="case.received", data={"case_id": case_id}))
        step = MockStep()

        mock_diagnosis = DiagnosisResult(
            root_cause_category=RootCauseCategory.SOFT_DECLINE,
            specific_reason="mock_reason",
            confidence_score=0.9,
            reasoning="Mock reasoning"
        )
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

            result = await inner_process_case_workflow(ctx, step)

            assert result["status"] == "policy_rejected"
            m_exec.assert_not_called()

            audit_logs = db.execute(select(models.AuditLog).where(models.AuditLog.case_id == case_id)).scalars().all()
            action_types = [log.action_type for log in audit_logs]
            assert "POLICY_EVALUATED" in action_types
            assert "ACTION_EXECUTED" not in action_types
    finally:
        db.close()
