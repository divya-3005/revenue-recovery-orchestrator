import pytest
from unittest.mock import MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models import Base, RecoveryCase, CaseStatus, CaseType, PaymentConfirmation, ApprovalStatus
from app.domain import (
    RecoveryCaseContext, DecisionResult, PolicyApprovedDecision, RecoveryActionType, ExecutionResult, ExecutionStatus
)
from app.execution.executor import RazorpayExecutor
from app.main import process_payment_confirmation

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

@pytest.fixture
def mocked_executor(monkeypatch):
    executor = RazorpayExecutor()
    executor.client = MagicMock()
    # Mock network call to return success instead of hitting Razorpay API
    def mock_create(*args, **kwargs):
        case = args[1] if hasattr(args[0], 'client') else args[0]
        approved_decision = args[2] if hasattr(args[0], 'client') else args[1]
        
        params = approved_decision.decision.action_parameters.copy()
        params["amount_charged_paise"] = case.amount_paise
        return ExecutionResult(
            status=ExecutionStatus.SUCCESS,
            action_taken=approved_decision.decision.recommended_action,
            reason="Mocked link created",
            external_reference_id="mock_pay_123",
            action_parameters_used=params
        )
    monkeypatch.setattr(executor, "_create_payment_link", mock_create)
    return executor


def test_invariant_retry_charge_unsupported(mocked_executor):
    """Invariant: Native RETRY_CHARGE returns UNSUPPORTED because we cannot trigger real test payments via Razorpay API."""
    executor = RazorpayExecutor()
    case_ctx = RecoveryCaseContext(
        id="case_123", case_type=CaseType.SUBSCRIPTION_FAILED, amount_paise=1000,
        currency="INR", customer_id="cust_1", status=CaseStatus.IN_PROGRESS,
        priority_score=100, raw_signal_payload={}, retry_count=0, cumulative_discount_paise=0
    )
    decision = DecisionResult(recommended_action=RecoveryActionType.RETRY_CHARGE, reasoning="test", confidence_score=0.9, action_parameters={"delay_hours": 0})
    approved = PolicyApprovedDecision(decision=decision, policy_reason="test", idempotency_key="key", requires_human_approval=False)
    
    result = mocked_executor.execute(case_ctx, approved)
    assert result.status.value == "unsupported"

def test_invariant_execution_success_does_not_equal_recovery(mocked_executor):
    """Invariant: Execution success != payment recovery. Execution of SEND_REMINDER creates a link but does not recover funds."""
    executor = RazorpayExecutor()
    case_ctx = RecoveryCaseContext(
        id="case_456", case_type=CaseType.SUBSCRIPTION_FAILED, amount_paise=1000,
        currency="INR", customer_id="cust_1", status=CaseStatus.IN_PROGRESS,
        priority_score=100, raw_signal_payload={"email": "test@test.com"}, retry_count=0, cumulative_discount_paise=0
    )
    decision = DecisionResult(recommended_action=RecoveryActionType.SEND_REMINDER, reasoning="test", confidence_score=0.9, action_parameters={"channel": "email"})
    approved = PolicyApprovedDecision(decision=decision, policy_reason="test", idempotency_key="key", requires_human_approval=False)
    
    result = mocked_executor.execute(case_ctx, approved)
    # The action succeeds (link created)
    assert result.status.value == "success"
    # But the case status remains unchanged (it does not become RECOVERED here)
    assert case_ctx.status != CaseStatus.RECOVERED

def test_invariant_only_verified_payment_confirmation_recovers_funds(db):
    """Invariant: Only verified payment confirmation can produce RECOVERED status."""
    case = RecoveryCase(
        id="case_789", case_type=CaseType.SUBSCRIPTION_FAILED, amount_paise=1000,
        currency="INR", customer_id="cust_1", status=CaseStatus.PAYMENT_PENDING, raw_signal_payload={}
    )
    db.add(case)
    db.commit()

    # Call the strict webhook helper
    result = process_payment_confirmation(db, "pay_123", case.id, 1000, "webhook")
    assert result["status"] == "recovered"
    
    db.refresh(case)
    assert case.status == CaseStatus.RECOVERED
    assert case.recovered_amount_paise == 1000

def test_invariant_payment_confirmation_idempotency_and_uniqueness(db):
    """Invariant: Payment confirmation is idempotent and cross-case unique based on payment_id."""
    case_a = RecoveryCase(
        id="case_A", case_type=CaseType.SUBSCRIPTION_FAILED, amount_paise=1000,
        currency="INR", customer_id="cust_1", status=CaseStatus.PAYMENT_PENDING, raw_signal_payload={}
    )
    case_b = RecoveryCase(
        id="case_B", case_type=CaseType.SUBSCRIPTION_FAILED, amount_paise=1000,
        currency="INR", customer_id="cust_1", status=CaseStatus.PAYMENT_PENDING, raw_signal_payload={}
    )
    db.add_all([case_a, case_b])
    db.commit()

    # Apply pay_123 to case A
    res1 = process_payment_confirmation(db, "pay_123", case_a.id, 1000, "webhook")
    assert res1["status"] == "recovered"

    from fastapi import HTTPException
    
    # Attempt to apply pay_123 to case B
    with pytest.raises(HTTPException) as excinfo:
        process_payment_confirmation(db, "pay_123", case_b.id, 1000, "webhook")
    
    # Must be rejected due to global payment idempotency cross-case conflict
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == "PAYMENT_ID_ALREADY_BOUND_TO_DIFFERENT_CASE"

    db.refresh(case_b)
    assert case_b.status != CaseStatus.RECOVERED

def test_invariant_approval_hash_verification(mocked_executor):
    """Invariant: Executor independently enforces approval gates and hash verification."""
    executor = RazorpayExecutor()
    case_ctx = RecoveryCaseContext(
        id="case_human", case_type=CaseType.SUBSCRIPTION_FAILED, amount_paise=1000000, # High value
        currency="INR", customer_id="cust_1", status=CaseStatus.IN_PROGRESS,
        priority_score=100, raw_signal_payload={"email": "test@test.com"}, retry_count=0, cumulative_discount_paise=0
    )
    
    decision = DecisionResult(recommended_action=RecoveryActionType.SEND_REMINDER, reasoning="test", confidence_score=0.9, action_parameters={"channel": "email"})
    approved = PolicyApprovedDecision(decision=decision, policy_reason="High value", idempotency_key="key", requires_human_approval=True)

    # 1. No approval provided
    res_no_approval = mocked_executor.execute(case_ctx, approved)
    assert res_no_approval.status.value == "failed"
    assert "Requires human approval" in res_no_approval.reason

    # 2. Approved, but hash mismatch
    case_ctx.approval_status = ApprovalStatus.APPROVED.value
    case_ctx.approved_decision_hash = "wrong_hash"
    res_bad_hash = mocked_executor.execute(case_ctx, approved)
    assert res_bad_hash.status.value == "failed"
    assert "hash mismatch" in res_bad_hash.reason

    # 3. Approved and hash matches
    valid_hash = decision.canonical_hash()
    case_ctx.approved_decision_hash = valid_hash
    case_ctx.approved_decision_id = decision.recommended_action.value
    
    res_good = mocked_executor.execute(case_ctx, approved)
    assert res_good.status.value == "success"
