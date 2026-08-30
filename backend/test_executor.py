"""
Tests for the RazorpayExecutor.

Verifies:
  - Payment link creation for RETRY_CHARGE
  - Unsupported action handling
  - Razorpay API failure handling
  - reference_id ≤ 40 chars
  - ESCALATE_TO_HUMAN and STOP actions (no API call)
  - TypeError on raw DecisionResult
"""

from unittest.mock import patch, MagicMock
from app.execution.executor import RazorpayExecutor, DryRunExecutor
from app.domain import (
    RecoveryCaseContext, PolicyApprovedDecision, DecisionResult,
    RecoveryActionType, ExecutionStatus
)
from app.models import CaseType, CaseStatus


def _make_case(**overrides):
    defaults = dict(
        id="case_123",
        case_type=CaseType.CHECKOUT_ABANDONED,
        amount_paise=100000,
        currency="INR",
        customer_id="cust_123",
        raw_signal_payload={"email": "test@example.com"},
        retry_count=0,
        status=CaseStatus.OPEN,
        cumulative_discount_paise=0,
    )
    defaults.update(overrides)
    return RecoveryCaseContext(**defaults)


def _make_approved(action, params):
    decision = DecisionResult(
        recommended_action=action,
        action_parameters=params,
        confidence_score=0.9,
        reasoning="Test"
    )
    return PolicyApprovedDecision(
        decision=decision,
        policy_reason="Policy passed",
        idempotency_key="idem_test"
    )


def test_razorpay_executor_success():
    case = _make_case()
    approved = _make_approved(RecoveryActionType.CREATE_PAYMENT_LINK, {"delay_hours": 0})

    executor = RazorpayExecutor()
    with patch.object(executor.client.payment_link, 'create') as mock_create:
        mock_create.return_value = {"id": "plink_test_123"}

        result = executor.execute(case, approved)

        assert result.status == ExecutionStatus.SUCCESS
        assert result.action_taken == RecoveryActionType.CREATE_PAYMENT_LINK
        assert result.external_reference_id == "plink_test_123"

        mock_create.assert_called_once()

        # Verify SDK call uses data= only (no headers kwarg)
        call_kwargs = mock_create.call_args[1]
        assert "data" in call_kwargs
        assert "headers" not in call_kwargs  # Bug fix verified

        # Verify reference_id ≤ 40 chars
        ref_id = call_kwargs["data"]["reference_id"]
        assert len(ref_id) <= 40


def test_razorpay_executor_offer_discount():
    case = _make_case(amount_paise=100000)
    approved = _make_approved(RecoveryActionType.OFFER_DISCOUNT, {"discount_percent": 10})

    executor = RazorpayExecutor()
    with patch.object(executor.client.payment_link, 'create') as mock_create:
        mock_create.return_value = {"id": "plink_discount_123"}
        
        result = executor.execute(case, approved)
        assert result.status == ExecutionStatus.SUCCESS
        assert result.action_taken == RecoveryActionType.OFFER_DISCOUNT
        assert result.external_reference_id == "plink_discount_123"
        
        # Original amount 100000, 10% discount = 10000, new amount = 90000
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["data"]["amount"] == 90000
        
        assert result.action_parameters_used["discount_applied_paise"] == 10000
        assert result.action_parameters_used["amount_charged_paise"] == 90000


def test_razorpay_executor_api_failure():
    case = _make_case()
    approved = _make_approved(RecoveryActionType.CREATE_PAYMENT_LINK, {"delay_hours": 0})

    executor = RazorpayExecutor()
    with patch.object(executor.client.payment_link, 'create', side_effect=Exception("Network timeout")):
        result = executor.execute(case, approved)
        assert result.status == ExecutionStatus.FAILED
        assert "Network timeout" in result.reason


def test_razorpay_executor_escalate_no_api_call():
    """ESCALATE_TO_HUMAN should succeed without calling Razorpay API."""
    case = _make_case()
    approved = _make_approved(RecoveryActionType.ESCALATE_TO_HUMAN, {})

    executor = RazorpayExecutor()
    with patch.object(executor.client.payment_link, 'create') as mock_create:
        result = executor.execute(case, approved)

        assert result.status == ExecutionStatus.SUCCESS
        assert "escalated" in result.reason.lower()
        mock_create.assert_not_called()  # No API call for internal action


def test_razorpay_executor_stop_no_api_call():
    """STOP should succeed without calling Razorpay API."""
    case = _make_case()
    approved = _make_approved(RecoveryActionType.STOP, {})

    executor = RazorpayExecutor()
    with patch.object(executor.client.payment_link, 'create') as mock_create:
        result = executor.execute(case, approved)

        assert result.status == ExecutionStatus.SUCCESS
        assert "closed" in result.reason.lower() or "stopped" in result.reason.lower()
        mock_create.assert_not_called()


def test_razorpay_executor_send_reminder():
    """SEND_REMINDER should create a payment link (like retry)."""
    case = _make_case()
    approved = _make_approved(RecoveryActionType.SEND_REMINDER, {"channel": "email"})

    executor = RazorpayExecutor()
    with patch.object(executor.client.payment_link, 'create') as mock_create:
        mock_create.return_value = {"id": "plink_reminder_123"}
        result = executor.execute(case, approved)

        assert result.status == ExecutionStatus.SUCCESS
        mock_create.assert_called_once()


def test_executor_rejects_raw_decision():
    """Executor MUST reject a raw DecisionResult (not wrapped in PolicyApprovedDecision)."""
    case = _make_case()
    raw_decision = DecisionResult(
        recommended_action=RecoveryActionType.RETRY_CHARGE,
        action_parameters={"delay_hours": 0},
        confidence_score=0.9,
        reasoning="Test"
    )

    for executor in [DryRunExecutor(), RazorpayExecutor()]:
        try:
            executor.execute(case, raw_decision)  # type: ignore
            assert False, "Should have raised TypeError"
        except TypeError as e:
            assert "PolicyApprovedDecision" in str(e)


def test_razorpay_executor_switch_rail():
    case = _make_case()
    approved = _make_approved(RecoveryActionType.SWITCH_RAIL, {"target_rail": "upi", "channel": "sms"})

    executor = RazorpayExecutor()
    with patch.object(executor.client.payment_link, 'create') as mock_create:
        mock_create.return_value = {"id": "plink_switch_123"}
        result = executor.execute(case, approved)

        assert result.status == ExecutionStatus.SUCCESS
        assert result.action_taken == RecoveryActionType.SWITCH_RAIL
        assert result.external_reference_id == "plink_switch_123"

        call_kwargs = mock_create.call_args[1]
        assert "UPI" in call_kwargs["data"]["description"]
        assert call_kwargs["data"]["notify"] == {"sms": 1, "email": 0}


if __name__ == "__main__":
    test_razorpay_executor_success()
    test_razorpay_executor_offer_discount()
    test_razorpay_executor_api_failure()
    test_razorpay_executor_escalate_no_api_call()
    test_razorpay_executor_stop_no_api_call()
    test_razorpay_executor_send_reminder()
    test_razorpay_executor_switch_rail()
    test_executor_rejects_raw_decision()
    print("SUCCESS: All executor tests passed.")
