"""
End-to-end tests for the Revenue Recovery Orchestrator MVP.

Tests the full pipeline: API → DB → Workflow → Diagnosis → Decision → Policy → Comms → Execution → Audit.
Uses MockStep to simulate Inngest's step.run (calls functions synchronously).
"""

import asyncio
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from app.main import app
from app.models import CaseType, CaseStatus
from app.database import SessionLocal
from app import models
from app.domain import (
    DiagnosisResult, RootCauseCategory, DecisionResult,
    RecoveryActionType, ExecutionStatus, PolicyEvaluationResult
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

async def run_e2e_happy_path():
    db = SessionLocal()
    try:
        # 1. Submit case via API
        with patch('app.main.inngest_client.send_sync') as mock_send:
            response = client.post("/api/v1/cases", json={
                "case_type": CaseType.CHECKOUT_ABANDONED.value,
                "amount_paise": 500000,
                "currency": "INR",
                "customer_id": "cust_e2e_happy",
                "payment_rail": "upi",
                "raw_signal_payload": {"merchant_id": "m_123"}
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
            recommended_action=RecoveryActionType.RETRY_CHARGE,
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

        # 4. Verify case status updated to AWAITING_PAYMENT
        db.expire_all()
        updated_case = db.query(models.RecoveryCase).filter(
            models.RecoveryCase.id == case_id
        ).first()
        assert updated_case.status == CaseStatus.AWAITING_PAYMENT

        print("PASS: E2E happy path")
    finally:
        db.close()


# ── Test 2: Unsafe AI decision blocked by policy ────────────────────────

async def run_e2e_policy_rejection():
    db = SessionLocal()
    try:
        with patch('app.main.inngest_client.send_sync'):
            response = client.post("/api/v1/cases", json={
                "case_type": CaseType.CHECKOUT_ABANDONED.value,
                "amount_paise": 1500000,
                "currency": "INR",
                "customer_id": "cust_e2e_unsafe",
                "payment_rail": "upi",
                "raw_signal_payload": {}
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
            logs = db.execute(select(models.AuditLog).where(
                models.AuditLog.case_id == case_id
            )).scalars().all()
            action_types = [log.action_type for log in logs]
            assert "POLICY_EVALUATED" in action_types
            assert "ACTION_EXECUTED" not in action_types

        # Verify case status is FAILED
        db.expire_all()
        case = db.query(models.RecoveryCase).filter(models.RecoveryCase.id == case_id).first()
        assert case.status == CaseStatus.FAILED

        print("PASS: E2E policy rejection")
    finally:
        db.close()


# ── Test 3: Razorpay failure is handled safely ──────────────────────────

async def run_e2e_razorpay_failure():
    db = SessionLocal()
    try:
        with patch('app.main.inngest_client.send_sync'):
            response = client.post("/api/v1/cases", json={
                "case_type": CaseType.CHECKOUT_ABANDONED.value,
                "amount_paise": 600000,
                "currency": "INR",
                "customer_id": "cust_e2e_fail",
                "payment_rail": "upi",
                "raw_signal_payload": {}
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
            recommended_action=RecoveryActionType.RETRY_CHARGE,
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

            # After 2 failed Razorpay calls (initial + 1 retry), retry_count hits MAX_RETRIES + 1 (2).
            # The 3rd attempt's policy check blocks it because retry_count (2) > MAX_RETRIES (1).
            # This is correct safety behavior: policy limits to exactly 1 retry loop.
            assert result["status"] == "policy_rejected"
            assert "Max retries" in result["reason"]

            # Verify failure audit entries exist from the first 3 attempts
            logs = db.execute(select(models.AuditLog).where(
                models.AuditLog.case_id == case_id
            )).scalars().all()
            exec_logs = [l for l in logs if l.action_type == "ACTION_EXECUTED"]
            assert len(exec_logs) > 0
            assert "Razorpay API failure" in exec_logs[0].reasoning

        print("PASS: E2E Razorpay failure")
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

async def run_e2e_communication():
    db = SessionLocal()
    try:
        with patch('app.main.inngest_client.send_sync'):
            response = client.post("/api/v1/cases", json={
                "case_type": CaseType.SUBSCRIPTION_FAILED.value,
                "amount_paise": 49900,
                "currency": "INR",
                "customer_id": "cust_e2e_comms",
                "payment_rail": "card",
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
        mock_decision = DecisionResult(
            recommended_action=RecoveryActionType.RETRY_CHARGE,
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
        logs = db.execute(select(models.AuditLog).where(
            models.AuditLog.case_id == case_id
        )).scalars().all()
        comms_logs = [l for l in logs if l.action_type == "COMMUNICATION_SENT"]
        assert len(comms_logs) == 1
        assert "insufficient funds" in comms_logs[0].reasoning  # the generated message

        print("PASS: E2E communication generation")
    finally:
        db.close()

# ── Test 5b: Webhook confirms payment ───────────────────────────────────

def test_webhook_confirms_payment():
    db = SessionLocal()
    try:
        # Create a case in AWAITING_PAYMENT state
        case = models.RecoveryCase(
            case_type=CaseType.SUBSCRIPTION_FAILED,
            status=CaseStatus.AWAITING_PAYMENT,
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
        
        payload = {
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {
                    "entity": {
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
        
        print("PASS: Webhook confirms payment")
    finally:
        db.close()


# ── Test 6: Escalation for high-value cases ─────────────────────────────

async def run_e2e_escalation():
    db = SessionLocal()
    try:
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

            assert result["status"] == "escalated"
            assert "Human approval" in result["reason"]

        # Verify case status is ESCALATED
        db.expire_all()
        case = db.query(models.RecoveryCase).filter(
            models.RecoveryCase.id == case_id
        ).first()
        assert case.status == CaseStatus.ESCALATED

        # Verify it shows in escalation endpoint
        esc_response = client.get("/api/v1/cases/escalated")
        esc_ids = [c["id"] for c in esc_response.json()]
        assert case_id in esc_ids

        print("PASS: E2E escalation")
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

    print("PASS: Audit trail endpoint")


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
    print("PASS: Analytics endpoint")


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
    print("PASS: Batch endpoint")


# ── Runner ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    asyncio.run(run_e2e_happy_path())
    asyncio.run(run_e2e_policy_rejection())
    asyncio.run(run_e2e_razorpay_failure())
    test_create_case_inngest_failure_still_commits()
    asyncio.run(run_e2e_communication())
    test_webhook_confirms_payment()
    asyncio.run(run_e2e_escalation())
    test_audit_trail_endpoint()
    test_analytics_endpoint()
    test_batch_endpoint()
    print("\nSUCCESS: All e2e tests passed!")
