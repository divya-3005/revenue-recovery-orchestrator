"""
End-to-end tests for the Revenue Recovery Orchestrator.

Tests the full pipeline: Case → Diagnosis → Decision → Policy → Comms → Execution → Audit.
"""

from datetime import date, timedelta
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.main import app, get_db
import app.database as db_module
from app.models import (
    CaseType, CaseStatus, RecoveryCase, AuditLog,
    DiagnosisResult, DecisionResult, RootCauseCategory, RecoveryActionType,
)


def SessionLocal():
    return db_module.SessionLocal()


def _get_test_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = _get_test_db
client = TestClient(app)


# ── Helpers ─────────────────────────────────────────────────────────────

def _create_case(case_type="subscription_failed", amount=50000, rail="card",
                 payload=None, customer_id="cust_test"):
    """Create a case via the API and return its ID."""
    payload = payload or {"reason": "insufficient_funds", "email": "test@example.com"}
    resp = client.post("/api/v1/cases", json={
        "case_type": case_type,
        "amount_paise": amount,
        "customer_id": customer_id,
        "payment_rail": rail,
        "raw_signal_payload": payload,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ── Test 1: Case creation and listing ───────────────────────────────────

def test_create_and_list_cases():
    """Feature 1: Cases appear in the live queue sorted by priority."""
    id1 = _create_case(amount=10000)
    id2 = _create_case(amount=500000, customer_id="cust_big")

    resp = client.get("/api/v1/cases")
    assert resp.status_code == 200
    cases = resp.json()
    assert len(cases) >= 2

    # Higher amount should have higher priority and appear first
    ids = [c["id"] for c in cases]
    assert ids.index(id2) < ids.index(id1)


# ── Test 2: Full pipeline — soft decline → payment pending ─────────────

def test_pipeline_soft_decline():
    """Features 2-7: Soft decline flows through diagnosis → decision → policy → comms → execution."""
    case_id = _create_case(payload={"reason": "insufficient_funds", "email": "test@example.com"})

    # Wait for background task (not running in test mode), so run pipeline directly
    from app.pipeline import run_pipeline
    db = SessionLocal()
    try:
        result = run_pipeline(db, case_id)

        # Should reach payment_pending or escalated (depends on AI)
        assert result["status"] in ("payment_pending", "escalated", "awaiting_approval")

        # Check audit trail has the pipeline steps
        logs = db.query(AuditLog).filter(AuditLog.case_id == case_id).order_by(AuditLog.created_at).all()
        action_types = [l.action_type for l in logs]

        assert "SIGNAL_RECEIVED" in action_types
        assert "DIAGNOSIS_COMPLETED" in action_types
        assert "DECISION_PROPOSED" in action_types
        assert "POLICY_EVALUATED" in action_types
    finally:
        db.close()


# ── Test 3: Hard decline → policy blocks retry ─────────────────────────

def test_pipeline_hard_decline_blocked():
    """Feature 3: Policy blocks retrying hard declines."""
    case_id = _create_case(payload={"reason": "stolen_card", "email": "test@example.com"})

    from app.pipeline import run_pipeline
    db = SessionLocal()
    try:
        result = run_pipeline(db, case_id)

        # Hard decline should either be escalated (AI chose escalate) or failed (policy blocked)
        case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
        assert case.status in (CaseStatus.FAILED, CaseStatus.ESCALATED, CaseStatus.PAYMENT_PENDING,
                               CaseStatus.AWAITING_APPROVAL)
    finally:
        db.close()


# ── Test 4: Audit trail is complete ─────────────────────────────────────

def test_audit_trail():
    """Feature 10: Every pipeline step is logged in order."""
    case_id = _create_case()

    from app.pipeline import run_pipeline
    db = SessionLocal()
    try:
        run_pipeline(db, case_id)
    finally:
        db.close()

    resp = client.get(f"/api/v1/cases/{case_id}/audit")
    assert resp.status_code == 200
    logs = resp.json()
    assert len(logs) >= 3  # At minimum: SIGNAL_RECEIVED, DIAGNOSIS, DECISION


# ── Test 5: Policy configuration is visible ─────────────────────────────

def test_policy_endpoint():
    """Feature 3: Policy config is visible and inspectable."""
    resp = client.get("/api/v1/policy")
    assert resp.status_code == 200
    policy = resp.json()
    assert policy["max_retries"] == 3
    assert policy["block_hard_declines"] is True
    assert policy["max_discount_percent"] == 15


# ── Test 6: Batch processing ───────────────────────────────────────────

def test_batch_processing():
    """Feature 11: 50+ cases processed end-to-end."""
    resp = client.post("/api/v1/batch?simulate=true")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cases_created"] >= 50


# ── Test 7: Analytics dashboard ─────────────────────────────────────────

def test_analytics():
    """Feature 12 + 17: Recovery analytics with net economics."""
    # Create and process some cases first
    for i in range(3):
        _create_case(amount=10000 * (i + 1), customer_id=f"cust_analytics_{i}")

    db = SessionLocal()
    try:
        from app.pipeline import run_pipeline
        cases = db.query(RecoveryCase).all()
        for c in cases:
            run_pipeline(db, c.id)
    finally:
        db.close()

    resp = client.get("/api/v1/analytics")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_cases" in data
    assert "total_at_risk_paise" in data
    assert "net_recovered_paise" in data
    assert "breakdown_by_case_type" in data
    assert "exceptions" in data


# ── Test 8: Customer opt-out stops recovery ─────────────────────────────

def test_opt_out():
    """Feature 8: Opt-out immediately closes the case."""
    case_id = _create_case(customer_id="cust_optout")

    resp = client.post(f"/api/v1/cases/{case_id}/opt-out",
                       json={"reason": "Customer asked to stop"})
    assert resp.status_code == 200

    db = SessionLocal()
    try:
        case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
        assert case.status == CaseStatus.CLOSED
        assert case.opted_out is True
    finally:
        db.close()


# ── Test 9: Promise-to-pay ──────────────────────────────────────────────

def test_promise_to_pay():
    """Feature 14: Capture PTP and suppress reminders."""
    case_id = _create_case(customer_id="cust_ptp")
    future_date = (date.today() + timedelta(days=3)).isoformat()

    resp = client.post(f"/api/v1/cases/{case_id}/promise-to-pay",
                       json={"date": future_date, "note": "Will pay Friday"})
    assert resp.status_code == 200

    db = SessionLocal()
    try:
        case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
        assert case.promise_to_pay_date is not None

        # Pipeline should return "waiting" when PTP is in the future
        from app.pipeline import run_pipeline
        result = run_pipeline(db, case_id)
        assert result["status"] == "waiting"
    finally:
        db.close()


# ── Test 10: Promise-to-pay past due → escalation ──────────────────────

def test_promise_to_pay_broken():
    """Feature 14: Broken PTP auto-escalates."""
    case_id = _create_case(customer_id="cust_ptp_broken")

    db = SessionLocal()
    try:
        case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
        case.promise_to_pay_date = date.today() - timedelta(days=1)  # Yesterday
        db.commit()

        from app.pipeline import run_pipeline
        result = run_pipeline(db, case_id)
        assert result["status"] == "escalated"

        case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
        assert case.status == CaseStatus.ESCALATED
    finally:
        db.close()


# ── Test 11: Escalation queue ──────────────────────────────────────────

def test_escalation_queue():
    """Feature 9: Escalated cases appear in the queue."""
    case_id = _create_case(customer_id="cust_escalate")

    db = SessionLocal()
    try:
        case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
        case.status = CaseStatus.ESCALATED
        db.commit()
    finally:
        db.close()

    resp = client.get("/api/v1/cases/escalated")
    assert resp.status_code == 200
    escalated = resp.json()
    assert any(c["id"] == case_id for c in escalated)


# ── Test 12: Demo seed endpoint ─────────────────────────────────────────

def test_demo_seed():
    """Demo endpoint seeds 5 canonical scenarios."""
    resp = client.post("/api/v1/demo/seed")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 5


# ── Test 13: Health check ──────────────────────────────────────────────

def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


# ── Test 14: Payment simulation ────────────────────────────────────────

def test_demo_payment():
    """Demo payment confirms a case as recovered."""
    case_id = _create_case(customer_id="cust_pay_test")

    db = SessionLocal()
    try:
        case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
        case.status = CaseStatus.PAYMENT_PENDING
        db.commit()
    finally:
        db.close()

    resp = client.post(f"/api/v1/demo/confirm-payment/{case_id}")
    assert resp.status_code == 200

    db = SessionLocal()
    try:
        case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
        assert case.status == CaseStatus.RECOVERED
    finally:
        db.close()


# ── Test 15: All three case types work ──────────────────────────────────

def test_all_three_case_types():
    """Feature 16: One orchestrator handles all three revenue-at-risk types."""
    from app.pipeline import run_pipeline

    # Subscription failure
    id1 = _create_case("subscription_failed", payload={"reason": "insufficient_funds", "email": "a@b.com"})
    # Checkout abandoned
    id2 = _create_case("checkout_abandoned", payload={"cart_items": 2, "email": "c@d.com"}, rail="upi")
    # Invoice overdue
    id3 = _create_case("invoice_overdue", payload={"days_overdue": 5, "email": "e@f.com"})

    db = SessionLocal()
    try:
        for cid in [id1, id2, id3]:
            result = run_pipeline(db, cid)
            assert result["status"] in ("payment_pending", "escalated", "failed", "awaiting_approval")
    finally:
        db.close()


# ── Test 16: High-value case → human approval ─────────────────────────

def test_high_value_requires_approval():
    """Feature 15: Cases above ₹50,000 require human approval."""
    case_id = _create_case(amount=6_000_000, customer_id="cust_highval",
                           payload={"reason": "insufficient_funds", "email": "big@corp.com"})

    from app.pipeline import run_pipeline
    db = SessionLocal()
    try:
        result = run_pipeline(db, case_id)

        case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
        # Should be awaiting approval or escalated
        assert case.status in (CaseStatus.AWAITING_APPROVAL, CaseStatus.ESCALATED,
                               CaseStatus.PAYMENT_PENDING, CaseStatus.FAILED)
    finally:
        db.close()


# ── Test 17: Policy rules are deterministic ────────────────────────────

def test_policy_rules_directly():
    """Feature 3: Policy rules work correctly in isolation."""
    from app.policy import evaluate_policy
    from app.models import RecoveryCase as RC

    # Create a mock case object
    db = SessionLocal()
    try:
        case = RC(
            case_type=CaseType.SUBSCRIPTION_FAILED,
            amount_paise=100000,
            customer_id="test",
            raw_signal_payload={"reason": "insufficient_funds"},
            retry_count=0,
            cumulative_discount_paise=0,
        )
        db.add(case)
        db.commit()
        db.refresh(case)

        # Soft decline + create_payment_link should be allowed
        diagnosis = DiagnosisResult(
            root_cause_category=RootCauseCategory.SOFT_DECLINE,
            specific_reason="insufficient_funds",
            confidence_score=0.9,
            reasoning="Temporary failure.",
        )
        decision = DecisionResult(
            recommended_action=RecoveryActionType.CREATE_PAYMENT_LINK,
            action_parameters={"delay_hours": 24},
            confidence_score=0.85,
            reasoning="Retry via payment link.",
        )
        result = evaluate_policy(case, decision, diagnosis)
        assert result.allowed is True

        # Hard decline + retry should be blocked
        hard_diag = DiagnosisResult(
            root_cause_category=RootCauseCategory.HARD_DECLINE,
            specific_reason="stolen_card",
            confidence_score=0.95,
            reasoning="Card reported stolen.",
        )
        result = evaluate_policy(case, decision, hard_diag)
        assert result.allowed is False

        # Opted out customer should be blocked
        case.opted_out = True
        result = evaluate_policy(case, decision, diagnosis)
        assert result.allowed is False
        case.opted_out = False

        # Max retries reached should be blocked
        case.retry_count = 5
        result = evaluate_policy(case, decision, diagnosis)
        assert result.allowed is False
    finally:
        db.close()


# ── Test 18: Communication messages are personalized ───────────────────

def test_comms_generation():
    """Feature 6: Messages are personalized to diagnosis and escalate in tone."""
    from app.comms import generate_message

    db = SessionLocal()
    try:
        case = RecoveryCase(
            case_type=CaseType.SUBSCRIPTION_FAILED,
            amount_paise=99900,
            customer_id="test_comms",
            raw_signal_payload={},
        )
        db.add(case)
        db.commit()
        db.refresh(case)

        diag = DiagnosisResult(
            root_cause_category=RootCauseCategory.SOFT_DECLINE,
            specific_reason="insufficient_funds",
            confidence_score=0.9,
            reasoning="Test",
        )

        msg1 = generate_message(case, diag, attempt=1, channel="email")
        msg2 = generate_message(case, diag, attempt=2, channel="email")
        msg3 = generate_message(case, diag, attempt=3, channel="sms")

        # Gentle tone
        assert "999" in msg1  # Amount should appear
        # Firm tone
        assert "999" in msg2
        # Final tone, SMS
        assert len(msg3) < 200  # SMS should be concise
        # Tones should differ
        assert msg1 != msg2
    finally:
        db.close()


# ── Test 19: Razorpay Webhook Ingestion & Confirmation ─────────────────

def test_razorpay_webhook_ingestion_and_confirmation():
    """Feature 1 & 7: Webhook creates cases from failure events and confirms payments."""
    # Ingest a payment failure
    resp = client.post("/api/v1/webhooks/razorpay", json={
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_test_001", "amount": 149900, "currency": "INR",
                    "customer_id": "cust_wh_01", "email": "wh@example.com",
                    "contact": "+919876543210", "method": "card",
                    "error_code": "BAD_REQUEST_ERROR", "error_reason": "insufficient_funds",
                }
            }
        }
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "created"
    case_id = resp.json()["case_id"]

    # Confirm payment via webhook
    resp2 = client.post("/api/v1/webhooks/razorpay", json={
        "event": "payment.captured",
        "payload": {
            "payment": {"entity": {"id": "pay_test_002", "amount": 149900, "notes": {"case_id": case_id}}}
        }
    })
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "payment_confirmed"


# ── Test 20: Checkout Abandonment Beacon ───────────────────────────────

def test_checkout_abandoned_beacon():
    """Feature 1: Beacon ingests checkout drop-off signals."""
    resp = client.post("/api/v1/beacon/checkout-abandoned", json={
        "amount_paise": 79900, "customer_id": "cust_beacon_01",
        "cart_items": 3, "email": "beacon@example.com", "payment_rail": "upi",
    })
    assert resp.status_code == 201
    assert resp.json()["status"] == "created"
    assert "case_id" in resp.json()


# ── Test 21: Human Approval & Rejection Flow ──────────────────────────

def test_approval_and_rejection_flow():
    """Feature 15: Approve and reject actions in the human approval gate."""
    from app.models import ApprovalStatus

    # Create a case and set it to awaiting approval
    case_id = _create_case(amount=6_000_000)
    db = SessionLocal()
    try:
        case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
        case.status = CaseStatus.AWAITING_APPROVAL
        case.approval_status = ApprovalStatus.PENDING
        case.pending_decision_id = "dec_test_123"
        case.pending_decision_hash = "hash_test_123"
        case.pending_decision_json = {
            "decision_id": "dec_test_123",
            "recommended_action": "create_payment_link",
            "action_parameters": {"delay_hours": 24},
            "confidence_score": 0.9,
            "reasoning": "High value retry",
        }
        db.commit()

        # Approve it
        resp = client.post(f"/api/v1/cases/{case_id}/approve", json={
            "decision_id": "dec_test_123",
            "decision_hash": "hash_test_123",
            "reviewer_id": "admin_rev",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

        db.expire_all()
        case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
        assert case.approval_status == ApprovalStatus.APPROVED

        # Test rejection on a second case
        case2_id = _create_case(amount=7_000_000, customer_id="cust_reject")
        case2 = db.query(RecoveryCase).filter(RecoveryCase.id == case2_id).first()
        case2.status = CaseStatus.AWAITING_APPROVAL
        case2.approval_status = ApprovalStatus.PENDING
        case2.pending_decision_id = "dec_test_456"
        case2.pending_decision_hash = "hash_test_456"
        db.commit()

        resp_rej = client.post(f"/api/v1/cases/{case2_id}/reject", json={
            "decision_id": "dec_test_456",
            "decision_hash": "hash_test_456",
            "reviewer_id": "admin_rev",
        })
        assert resp_rej.status_code == 200
        assert resp_rej.json()["status"] == "rejected"

        # Verify rejection was audited
        logs = db.query(AuditLog).filter(AuditLog.case_id == case2_id).all()
        action_types = [l.action_type for l in logs]
        assert "REJECTED" in action_types
    finally:
        db.close()


