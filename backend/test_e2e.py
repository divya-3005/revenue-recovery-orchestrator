"""
End-to-end tests for the Revenue Recovery Orchestrator MVP.

Tests the full pipeline: API → DB → Workflow → Diagnosis → Decision → Policy → Comms → Execution → Audit.
Uses MockStep to simulate Inngest's step.run (calls functions synchronously).
"""

import asyncio
import pytest
import random
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from app.main import app
from app.models import CaseType, CaseStatus
from app import database
from app.database import get_db

def SessionLocal():
    return database.SessionLocal()

def override_get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db
from app import models
from app.db.audit_repository import update_case_status
from app.domain import (
    DiagnosisResult, RootCauseCategory, DecisionResult,
    RecoveryActionType, ExecutionStatus, PolicyEvaluationResult,
    ExecutionResult
)
from app.workflows.case_workflow import inner_process_case_workflow
from inngest import Event
from sqlalchemy import select

client = TestClient(app)


class SuspendWorkflow(Exception):
    pass

class MockStep:
    """Simulates Inngest step.run — calls function synchronously, no memoization."""
    async def run(self, step_id, fn, **kwargs):
        return fn()
        
    async def sleep(self, step_id, duration):
        raise SuspendWorkflow("awaiting_payment")

class MockContext:
    def __init__(self, event):
        self.event = event


# ── Test 1: Full happy path (soft decline → retry → awaiting payment) ────────────

@pytest.mark.asyncio
async def test_e2e_happy_path():
    # 1. Submit case via API
    with patch('app.main.inngest_client.send_sync') as mock_send:
        response = client.post("/api/v1/cases", json={
            "case_type": CaseType.CHECKOUT_ABANDONED.value,
            "amount_paise": 500000,
            "currency": "INR",
            "customer_id": "cust_e2e_happy",
            "payment_rail": "upi",
            "raw_signal_payload": {"merchant_id": "m_123", "email": "test@example.com"}
        })
        assert response.status_code == 201
        case_id = response.json()["id"]

        # Verify event was dispatched
        assert mock_send.call_count == 1
        event = mock_send.call_args[0][0]
        assert event.name == "case.received"
        assert event.data["case_id"] == case_id

    # 2. Run the workflow (simulating Inngest worker)
    ctx = MockContext(event=Event(name="case.received", data={"case_id": case_id}))
    step = MockStep()

    mock_diagnosis = DiagnosisResult(
        root_cause_category=RootCauseCategory.SOFT_DECLINE,
        specific_reason="insufficient_funds",
        confidence_score=0.9,
        reasoning="Customer lacks funds."
    )
    mock_decision = DecisionResult(
        recommended_action=RecoveryActionType.CREATE_PAYMENT_LINK,
        action_parameters={"delay_hours": 24},
        confidence_score=0.9,
        reasoning="Retrying is best."
    )

    with patch('app.ai.provider.GeminiProvider.ask_structured', side_effect=[mock_diagnosis, mock_decision]), \
         patch('razorpay.Client') as mock_rz_class:

        mock_rz = mock_rz_class.return_value
        mock_rz.payment_link.create.return_value = {"id": "plink_test_ok"}

        try:
            await inner_process_case_workflow(ctx, step)
            assert False, "Should have suspended at sleep"
        except SuspendWorkflow as e:
            assert str(e) == "awaiting_payment"

        mock_rz.payment_link.create.assert_called_once()

        # Verify reference_id is ≤ 40 chars
        call_data = mock_rz.payment_link.create.call_args[1]["data"]
        assert len(call_data["reference_id"]) <= 40

    # 3. Verify audit trail
    db = SessionLocal()
    try:
        logs = db.execute(select(models.AuditLog).where(
            models.AuditLog.case_id == case_id
        )).scalars().all()
        action_types = [log.action_type for log in logs]
        assert "SIGNAL_RECEIVED" in action_types
        assert "DIAGNOSIS_COMPLETED" in action_types
        assert "DECISION_PROPOSED" in action_types
        assert "POLICY_EVALUATED" in action_types
        assert "COMMUNICATION_SENT" in action_types
        assert "ACTION_EXECUTED" in action_types

        # 4. Verify case status updated to PAYMENT_PENDING
        updated_case = db.query(models.RecoveryCase).filter(
            models.RecoveryCase.id == case_id
        ).first()
        assert updated_case.status == CaseStatus.PAYMENT_PENDING
    finally:
        db.close()


# ── Test 2: Unsafe AI decision blocked by policy ────────────────────────

@pytest.mark.asyncio
async def test_e2e_policy_rejection():
    with patch('app.main.inngest_client.send_sync'):
        response = client.post("/api/v1/cases", json={
            "case_type": CaseType.CHECKOUT_ABANDONED.value,
            "amount_paise": 1500000,
            "currency": "INR",
            "customer_id": "cust_e2e_unsafe",
            "payment_rail": "enach",
            "raw_signal_payload": {"email": "test@example.com"}
        })
        case_id = response.json()["id"]

    ctx = MockContext(event=Event(name="case.received", data={"case_id": case_id}))
    step = MockStep()

    mock_diagnosis = DiagnosisResult(
        root_cause_category=RootCauseCategory.SOFT_DECLINE,
        specific_reason="insufficient_funds",
        confidence_score=0.9,
        reasoning="Test."
    )
    mock_decision_unsafe = DecisionResult(
        recommended_action=RecoveryActionType.OFFER_DISCOUNT,
        action_parameters={"discount_percent": 50},  # exceeds 15% cap
        confidence_score=0.9,
        reasoning="Too much discount"
    )

    with patch('app.ai.provider.GeminiProvider.ask_structured',
                side_effect=[mock_diagnosis, mock_decision_unsafe]):
        result = await inner_process_case_workflow(ctx, step)

        assert result["status"] == "policy_rejected"

    # Verify no execution happened
    db = SessionLocal()
    try:
        logs = db.execute(select(models.AuditLog).where(
            models.AuditLog.case_id == case_id
        )).scalars().all()
        action_types = [log.action_type for log in logs]
        assert "POLICY_EVALUATED" in action_types
        assert "ACTION_EXECUTED" not in action_types

        # Verify case status is FAILED
        case = db.query(models.RecoveryCase).filter(models.RecoveryCase.id == case_id).first()
        assert case.status == CaseStatus.FAILED
    finally:
        db.close()


# ── Test 3: Razorpay failure is handled safely ──────────────────────────

@pytest.mark.asyncio
async def test_e2e_razorpay_failure():
    with patch('app.main.inngest_client.send_sync'):
        response = client.post("/api/v1/cases", json={
            "case_type": CaseType.CHECKOUT_ABANDONED.value,
            "amount_paise": 600000,
            "currency": "INR",
            "customer_id": "cust_e2e_fail",
            "payment_rail": "upi",
            "raw_signal_payload": {"email": "test@example.com"}
        })
        case_id = response.json()["id"]

    ctx = MockContext(event=Event(name="case.received", data={"case_id": case_id}))
    step = MockStep()

    mock_diagnosis = DiagnosisResult(
        root_cause_category=RootCauseCategory.SOFT_DECLINE,
        specific_reason="insufficient_funds",
        confidence_score=0.9,
        reasoning="Test."
    )
    mock_decision = DecisionResult(
        recommended_action=RecoveryActionType.CREATE_PAYMENT_LINK,
        action_parameters={"delay_hours": 24},
        confidence_score=0.9,
        reasoning="Retry"
    )

    with patch('app.ai.provider.GeminiProvider.ask_structured',
                side_effect=[mock_diagnosis, mock_decision] * 5), \
         patch('razorpay.Client') as mock_rz_class:

        mock_rz = mock_rz_class.return_value
        mock_rz.payment_link.create.side_effect = Exception("Razorpay 500 Internal Error")

        result = await inner_process_case_workflow(ctx, step)

        assert result["status"] == "policy_rejected"
        assert "Max retries" in result["reason"]

    # Verify failure audit entries exist from the first 3 attempts
    db = SessionLocal()
    try:
        logs = db.execute(select(models.AuditLog).where(
            models.AuditLog.case_id == case_id
        )).scalars().all()
        exec_logs = [l for l in logs if l.action_type == "ACTION_EXECUTED"]
        assert len(exec_logs) > 0
        assert "Razorpay API failure" in exec_logs[0].reasoning
    finally:
        db.close()


# ── Test 4: Inngest dispatch failure rolls back ─────────────────────────

def test_create_case_inngest_failure_still_commits():
    """
    After the fix, db.commit() happens BEFORE send_sync().
    So if send_sync fails, the case still exists in DB (safe: can be retried later).
    """
    with patch('app.main.inngest_client.send_sync', side_effect=Exception("Inngest network error")):
        response = client.post("/api/v1/cases", json={
            "case_type": CaseType.CHECKOUT_ABANDONED.value,
            "amount_paise": 800000,
            "currency": "INR",
            "customer_id": "cust_inngest_fail_v2",
            "raw_signal_payload": {}
        })
        # Should succeed because commit happens before send_sync
        assert response.status_code == 201
        case_id = response.json()["id"]

        # Case SHOULD exist in DB (committed before event dispatch)
        db = SessionLocal()
        try:
            case = db.query(models.RecoveryCase).filter(
                models.RecoveryCase.id == case_id
            ).first()
            assert case is not None  # Case was committed
        finally:
            db.close()


# ── Test 5: Communication generation in workflow ────────────────────────

@pytest.mark.asyncio
async def test_e2e_communication():
    with patch('app.main.inngest_client.send_sync'):
        response = client.post("/api/v1/cases", json={
            "case_type": CaseType.SUBSCRIPTION_FAILED.value,
            "amount_paise": 49900,
            "currency": "INR",
            "customer_id": "cust_e2e_comms",
            "payment_rail": "card",
            "raw_signal_payload": {"reason": "insufficient_funds", "email": "test@example.com"}
        })
        case_id = response.json()["id"]

    ctx = MockContext(event=Event(name="case.received", data={"case_id": case_id}))
    step = MockStep()

    mock_diagnosis = DiagnosisResult(
        root_cause_category=RootCauseCategory.SOFT_DECLINE,
        specific_reason="insufficient_funds",
        confidence_score=0.9,
        reasoning="Funds issue."
    )
    mock_decision = DecisionResult(
        recommended_action=RecoveryActionType.CREATE_PAYMENT_LINK,
        action_parameters={"delay_hours": 24},
        confidence_score=0.9,
        reasoning="Retry"
    )

    with patch('app.ai.provider.GeminiProvider.ask_structured',
                side_effect=[mock_diagnosis, mock_decision]), \
         patch('razorpay.Client') as mock_rz_class:

        mock_rz = mock_rz_class.return_value
        mock_rz.payment_link.create.return_value = {"id": "plink_comms_test"}

        try:
            await inner_process_case_workflow(ctx, step)
            assert False, "Should have suspended at sleep"
        except SuspendWorkflow:
            pass

    # Verify communication was logged
    db = SessionLocal()
    try:
        logs = db.execute(select(models.AuditLog).where(
            models.AuditLog.case_id == case_id
        )).scalars().all()
        comms_logs = [l for l in logs if l.action_type == "COMMUNICATION_SENT"]
        assert len(comms_logs) == 1
        assert "insufficient funds" in comms_logs[0].reasoning  # the generated message
    finally:
        db.close()

# ── Test 5b: Webhook confirms payment ───────────────────────────────────

def test_webhook_confirms_payment():
    db = SessionLocal()
    try:
        # Create a case in PAYMENT_PENDING state
        case = models.RecoveryCase(
            case_type=CaseType.SUBSCRIPTION_FAILED,
            status=CaseStatus.PAYMENT_PENDING,
            amount_paise=50000,
            currency="INR",
            customer_id="cust_webhook_test",
            raw_signal_payload={}
        )
        db.add(case)
        db.commit()
        db.refresh(case)
        
        # Send payment_link.paid webhook
        webhook_secret = "test_secret"
        import os
        import hmac
        import hashlib
        import json
        import random
        unique_id = f"plink_webhook_{random.randint(10000, 99999)}"
        payload = {
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {
                    "entity": {
                        "id": unique_id,
                        "amount_paid": 50000,
                        "notes": {
                            "case_id": case.id
                        }
                    }
                }
            }
        }
        body = json.dumps(payload).encode('utf-8')
        sig = hmac.new(
            key=webhook_secret.encode('utf-8'),
            msg=body,
            digestmod=hashlib.sha256
        ).hexdigest()
        
        with patch.dict(os.environ, {"RAZORPAY_WEBHOOK_SECRET": webhook_secret}):
            response = client.post(
                "/webhooks/razorpay", 
                content=body, 
                headers={"x-razorpay-signature": sig}
            )
            assert response.status_code == 200
            assert response.json()["status"] == "recovered"
            
        # Verify status changed
        db.refresh(case)
        assert case.status == CaseStatus.RECOVERED
        
        # Verify audit log
        logs = db.execute(select(models.AuditLog).where(
            models.AuditLog.case_id == case.id
        )).scalars().all()
        assert any(l.action_type == "PAYMENT_CONFIRMED" for l in logs)

    finally:
        db.close()


# ── Test 6: Escalation for high-value cases ─────────────────────────────

@pytest.mark.asyncio
async def test_e2e_escalation():
    with patch('app.main.inngest_client.send_sync'):
        response = client.post("/api/v1/cases", json={
            "case_type": CaseType.SUBSCRIPTION_FAILED.value,
            "amount_paise": 7000000,  # 70,000 INR — above 50k threshold
            "currency": "INR",
            "customer_id": "cust_e2e_escalation",
            "payment_rail": "enach",
            "raw_signal_payload": {"reason": "insufficient_funds"}
        })
        case_id = response.json()["id"]

    ctx = MockContext(event=Event(name="case.received", data={"case_id": case_id}))
    step = MockStep()

    mock_diagnosis = DiagnosisResult(
        root_cause_category=RootCauseCategory.SOFT_DECLINE,
        specific_reason="insufficient_funds",
        confidence_score=0.9,
        reasoning="Funds issue."
    )
    # AI proposes retry — but policy will block it for high-value cases
    mock_decision = DecisionResult(
        recommended_action=RecoveryActionType.RETRY_CHARGE,
        action_parameters={"delay_hours": 24},
        confidence_score=0.9,
        reasoning="Retry"
    )

    with patch('app.ai.provider.GeminiProvider.ask_structured',
                side_effect=[mock_diagnosis, mock_decision]):
        result = await inner_process_case_workflow(ctx, step)

        assert result["status"] == "awaiting_approval"
        assert "Human approval" in result["reason"]

    # Verify case status is AWAITING_APPROVAL
    db = SessionLocal()
    try:
        case = db.query(models.RecoveryCase).filter(
            models.RecoveryCase.id == case_id
        ).first()
        assert case.status == CaseStatus.AWAITING_APPROVAL

        # Verify it shows in escalation endpoint
        esc_response = client.get("/api/v1/cases/escalated")
        esc_ids = [c["id"] for c in esc_response.json()]
        assert case_id in esc_ids
    finally:
        db.close()


# ── Test 7: Audit trail endpoint ────────────────────────────────────────

def test_audit_trail_endpoint():
    with patch('app.main.inngest_client.send_sync'):
        response = client.post("/api/v1/cases", json={
            "case_type": CaseType.CHECKOUT_ABANDONED.value,
            "amount_paise": 100000,
            "currency": "INR",
            "customer_id": "cust_audit_test",
            "raw_signal_payload": {}
        })
        case_id = response.json()["id"]

    audit_resp = client.get(f"/api/v1/cases/{case_id}/audit")
    assert audit_resp.status_code == 200
    logs = audit_resp.json()
    assert len(logs) >= 1
    assert logs[0]["action_type"] == "SIGNAL_RECEIVED"

    # Non-existent case returns 404
    bad_resp = client.get("/api/v1/cases/fake-id/audit")
    assert bad_resp.status_code == 404


# ── Test 8: Analytics endpoint ──────────────────────────────────────────

def test_analytics_endpoint():
    response = client.get("/api/v1/analytics")
    assert response.status_code == 200
    data = response.json()
    assert "total_cases" in data
    assert "recovery_rate_percent" in data
    assert "breakdown_by_case_type" in data
    assert "breakdown_by_status" in data
    assert "exceptions" in data


# ── Test 9: Batch endpoint (Feature 11) ─────────────────────────────────

def test_batch_endpoint():
    with patch('app.main.inngest_client.send_sync'):
        response = client.post("/api/v1/batch")
        assert response.status_code == 200
        data = response.json()
        assert data["cases_created"] == 50
        assert len(data["case_ids"]) == 50

    # Verify analytics reflects the batch
    analytics = client.get("/api/v1/analytics").json()
    assert analytics["total_cases"] >= 50
@pytest.mark.asyncio
async def test_approve_concurrency_and_execution():
    """Test Path A approval with concurrency lock and execution."""
    db = SessionLocal()
    try:
        # Create a case in ESCALATED state with pending decision
        case_id = f"test_approve_{random.randint(1000, 9999)}"
        db_case = models.RecoveryCase(
            id=case_id,
            case_type=CaseType.SUBSCRIPTION_FAILED,
            amount_paise=10000000, # 100k INR
            currency="INR",
            customer_id="cust_high_value",
            status=CaseStatus.AWAITING_APPROVAL,
            approval_status=models.ApprovalStatus.PENDING,
            priority_score=10000000,
            raw_signal_payload={},
            pending_decision_json={
                "recommended_action": "offer_discount",
                "confidence_score": 0.9,
                "reasoning": "Offer discount",
                "action_parameters": {"discount_percent": 10}
            },
            pending_diagnosis_json={
                "root_cause_category": "friction",
                "specific_reason": "unknown",
                "confidence_score": 0.9,
                "reasoning": "Test"
            }
        )
        db.add(db_case)
        db.commit()

        # Fire two concurrent approve calls
        with patch('app.execution.executor.RazorpayExecutor.execute') as mock_exec, \
             patch('app.main.inngest_client.send_sync') as mock_send:
            
            mock_exec.return_value = ExecutionResult(
                status=ExecutionStatus.SUCCESS,
                action_taken=RecoveryActionType.OFFER_DISCOUNT,
                reason="Simulated success",
                action_parameters_used={"discount_applied_paise": 1000000}
            )

            import json
            import hashlib
            from app.domain import DecisionResult
            dec = DecisionResult(
                recommended_action="offer_discount",
                confidence_score=0.9,
                reasoning="Offer discount",
                action_parameters={"discount_percent": 10}
            )
            valid_hash = dec.canonical_hash()

            # Update the mock DB case with the canonical hash
            db_case.approved_decision_hash = valid_hash
            db.commit()

            response1 = client.post(f"/api/v1/cases/{case_id}/approve", json={"decision_hash": valid_hash})
            assert response1.status_code == 200
            
            response2 = client.post(f"/api/v1/cases/{case_id}/approve", json={"decision_hash": valid_hash})
            assert response2.status_code == 409 # Already claimed/approved and no longer valid

            # Verify execute event triggered
            assert mock_send.call_count == 1
            assert mock_send.call_args[0][0].name == "case.execute_approved"

            db.refresh(db_case)
            assert db_case.approval_status == models.ApprovalStatus.APPROVED
            # The execution task will transition to PAYMENT_PENDING
    finally:
        db.close()

@pytest.mark.asyncio
async def test_close_path_b():
    """Test Path B escalation closure."""
    db = SessionLocal()
    try:
        case_id = f"test_close_{random.randint(1000, 9999)}"
        db_case = models.RecoveryCase(
            id=case_id,
            case_type=CaseType.SUBSCRIPTION_FAILED,
            amount_paise=50000,
            currency="INR",
            customer_id="cust_decline",
            status=CaseStatus.ESCALATED,
            priority_score=50000,
            raw_signal_payload={},
            # No pending decision (Path B)
        )
        db.add(db_case)
        db.commit()

        response = client.post(f"/api/v1/cases/{case_id}/close")
        assert response.status_code == 200
        
        db.refresh(db_case)
        assert db_case.status == CaseStatus.CLOSED
    finally:
        db.close()


def test_update_case_status_block_reentry_from_awaiting_approval():
    db = SessionLocal()
    try:
        case = models.RecoveryCase(
            id=f"case_approval_lock_regression_{random.randint(1000, 9999)}",
            case_type=CaseType.SUBSCRIPTION_FAILED,
            amount_paise=200000,
            currency="INR",
            customer_id="cust_regression",
            status=CaseStatus.AWAITING_APPROVAL,
            raw_signal_payload={},
        )
        db.add(case)
        db.commit()

        updated = update_case_status(db, case.id, CaseStatus.IN_PROGRESS)
        assert updated is False

        db.refresh(case)
        assert case.status == CaseStatus.AWAITING_APPROVAL
    finally:
        db.close()


def test_close_endpoint_allows_awaiting_approval_case():
    db = SessionLocal()
    try:
        case = models.RecoveryCase(
            id=f"case_close_waiting_regression_{random.randint(1000, 9999)}",
            case_type=CaseType.INVOICE_OVERDUE,
            amount_paise=350000,
            currency="INR",
            customer_id="cust_close_waiting",
            status=CaseStatus.AWAITING_APPROVAL,
            raw_signal_payload={"email": "user@example.com"},
        )
        db.add(case)
        db.commit()
        case_id = case.id
    finally:
        db.close()

    response = client.post(f"/api/v1/cases/{case_id}/close")
    assert response.status_code == 200
    assert response.json()["status"] == "closed"

    db = SessionLocal()
    try:
        updated = db.query(models.RecoveryCase).filter(models.RecoveryCase.id == case_id).first()
        assert updated.status == CaseStatus.CLOSED
    finally:
        db.close()


def test_invoice_overdue_signal_creates_case():
    payload = {
        "invoice_id": f"inv_test_{random.randint(1000, 9999)}",
        "customer_id": "cust_inv_test",
        "amount_paise": 500000,
        "currency": "INR",
        "days_overdue": 5,
        "due_date": "2026-08-25",
        "invoice_number": "INV-8888"
    }
    response = client.post("/api/v1/signals/invoice-overdue", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "created"
    assert "case_id" in data

    db = SessionLocal()
    try:
        case = db.query(models.RecoveryCase).filter(models.RecoveryCase.id == data["case_id"]).first()
        assert case is not None
        assert case.case_type == CaseType.INVOICE_OVERDUE
        assert case.amount_paise == 500000
    finally:
        db.close()


def test_customer_opt_out_endpoint():
    db = SessionLocal()
    try:
        case = models.RecoveryCase(
            id=f"case_opt_out_{random.randint(1000, 9999)}",
            case_type=CaseType.CHECKOUT_ABANDONED,
            amount_paise=100000,
            currency="INR",
            customer_id="cust_opt_out",
            status=CaseStatus.IN_PROGRESS,
            raw_signal_payload={"email": "optout@example.com"},
        )
        db.add(case)
        db.commit()
        case_id = case.id
    finally:
        db.close()

    response = client.post(f"/api/v1/cases/{case_id}/opt-out", json={"reason": "Stop contacting me"})
    assert response.status_code == 200
    assert response.json()["status"] == "opted_out"
    assert response.json()["case_status"] == "closed"

    db = SessionLocal()
    try:
        updated = db.query(models.RecoveryCase).filter(models.RecoveryCase.id == case_id).first()
        assert updated.opted_out is True
        assert updated.status == CaseStatus.CLOSED
    finally:
        db.close()

