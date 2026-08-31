"""
Regression tests for two logic bugs found in commit bbc26cb
("fix: final audit and simplify revenue recovery flow").

1. policy.py Rule 6 (max retries) uses >= instead of >, which blocks the
   pipeline's own intended final attempt one iteration early, and routes
   the case to FAILED (policy block) instead of ESCALATED (attempts
   exhausted) — undermining Feature 9 (Escalation to Human) for exactly
   the cases that most need it: ones that failed every retry.

2. main.py's get_analytics() double-subtracts cumulative_discount_paise:
   once implicitly (because _simulate_payment now stores
   recovered_amount_paise net of discount) and once explicitly (the
   `net = recovered_paise - discounts - comms_cost` line). This silently
   under-reports net_recovered_paise for every discounted+recovered case
   by exactly the discount amount — corrupting Feature 17, the one metric
   the spec calls out as the differentiator.
"""
from unittest.mock import patch

from tests.test_pipeline import SessionLocal, client, _create_case
from app.models import (
    CaseStatus, RecoveryCase, CaseType, DiagnosisResult, DecisionResult,
    RootCauseCategory, RecoveryActionType, ExecutionResult, ExecutionStatus,
)


def _create_case_direct(case_type="checkout_abandoned", amount=100_000, payload=None):
    """Insert a case directly via the ORM — skips the live endpoint, which
    would otherwise kick off its own background pipeline run before this
    test gets a chance to patch diagnose/decide."""
    db = SessionLocal()
    try:
        case = RecoveryCase(
            case_type=CaseType(case_type),
            amount_paise=amount,
            customer_id="cust_direct",
            raw_signal_payload=payload or {},
        )
        db.add(case)
        db.commit()
        db.refresh(case)
        return case.id
    finally:
        db.close()


def test_max_retries_gets_full_attempt_budget_and_escalates():
    """
    POLICY['max_retries'] = 3, so run_pipeline computes
    max_attempts = 4 ("initial try + retries"). A case whose execution
    fails every single time should get all 4 real execution attempts,
    then land in ESCALATED (Feature 9) — not FAILED after only 3 attempts
    via a policy rejection that never even calls the executor.
    """
    case_id = _create_case_direct(
        case_type="subscription_failed", amount=50000,
        payload={"reason": "insufficient_funds"},
    )

    always_fail = ExecutionResult(
        status=ExecutionStatus.FAILED,
        action_taken=RecoveryActionType.CREATE_PAYMENT_LINK,
        reason="Simulated Razorpay outage.",
    )

    db = SessionLocal()
    try:
        with patch("app.pipeline.Executor.execute", return_value=always_fail):
            from app.pipeline import run_pipeline
            run_pipeline(db, case_id)

        case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()

        # All 4 attempts should have actually been executed. (retry_count
        # only counts continued retries, so it correctly reads 3 here — the
        # real signal is how many times the executor actually ran.)
        from app.models import AuditLog
        exec_count = db.query(AuditLog).filter(
            AuditLog.case_id == case_id, AuditLog.action_type == "ACTION_EXECUTED"
        ).count()
        assert exec_count == 4, (
            f"Expected 4 real execution attempts (initial try + 3 retries), "
            f"got {exec_count}. A premature policy block likely stopped the "
            f"executor from ever being called on the final attempt."
        )

        # The case should be ESCALATED (attempts exhausted -> human
        # review), not FAILED (policy blocked an action outright).
        assert case.status == CaseStatus.ESCALATED, (
            f"Expected ESCALATED after exhausting all attempts, got "
            f"{case.status}. A premature policy block on the final "
            f"attempt routes it to FAILED instead, skipping Feature 9's "
            f"human-escalation path for cases that failed every retry."
        )
    finally:
        db.close()


def test_net_recovered_does_not_double_count_discount():
    """
    Feature 17: net_recovered_paise should equal what was actually
    collected from the customer, minus comms cost. It should NOT also
    subtract the discount a second time — the discount is already the
    reason recovered_amount_paise is lower than amount_paise.
    """
    case_id = _create_case_direct(
        case_type="checkout_abandoned", amount=100_000,
        payload={"is_repeat_customer": True, "cart_value": 100_000},
    )

    friction_diag = DiagnosisResult(
        root_cause_category=RootCauseCategory.FRICTION,
        specific_reason="cart_abandoned",
        confidence_score=0.9,
        reasoning="Repeat customer abandoned a high-value cart.",
    )
    discount_decision = DecisionResult(
        recommended_action=RecoveryActionType.OFFER_DISCOUNT,
        action_parameters={"discount_percent": 10},
        confidence_score=0.9,
        reasoning="10% discount to recover a high-value abandoned cart.",
    )

    db = SessionLocal()
    try:
        with patch("app.pipeline.diagnose", return_value=friction_diag), \
             patch("app.pipeline.decide", return_value=discount_decision):
            from app.pipeline import run_pipeline
            run_pipeline(db, case_id)

        case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
        assert case.status == CaseStatus.PAYMENT_PENDING
        assert case.cumulative_discount_paise == 10_000  # 10% of 100,000

        from app.main import _simulate_payment
        _simulate_payment(db, case_id)
        db.refresh(case)

        # Customer actually paid amount - discount = 90,000
        assert case.recovered_amount_paise == 90_000
    finally:
        db.close()

    resp = client.get("/api/v1/analytics")
    data = resp.json()

    comms_cost = data["total_comms_cost_paise"]
    expected_net = 90_000 - comms_cost  # what was collected, minus comms — discount already reflected
    assert data["net_recovered_paise"] == expected_net, (
        f"Expected net_recovered_paise == {expected_net} (90,000 actually "
        f"collected minus {comms_cost} comms cost). Got "
        f"{data['net_recovered_paise']} — the discount is being "
        f"subtracted a second time on top of the already-discounted "
        f"recovered_amount_paise."
    )
