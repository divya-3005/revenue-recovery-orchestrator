from app.policy import evaluate_policy, RecoveryActionType, PolicyConfig
from fastapi.testclient import TestClient
from app.main import app

from app.domain import RecoveryCaseContext, DecisionResult, DiagnosisResult, RootCauseCategory
from app.models import CaseType, CaseStatus
import uuid

def create_mock_domain_case(amount_paise, raw_signal_payload=None, retry_count=0, cumulative_discount=0):
    return RecoveryCaseContext(
        id=str(uuid.uuid4()),
        case_type=CaseType.SUBSCRIPTION_FAILED,
        status=CaseStatus.OPEN,
        amount_paise=amount_paise,
        currency="INR",
        customer_id="cust-test",
        raw_signal_payload=raw_signal_payload or {},
        retry_count=retry_count,
        cumulative_discount_paise=cumulative_discount
    )

def create_mock_decision(action: RecoveryActionType, params: dict = None) -> DecisionResult:
    return DecisionResult(
        recommended_action=action,
        action_parameters=params or {},
        confidence_score=0.9,
        reasoning="Test"
    )

def create_mock_diagnosis(category: RootCauseCategory = RootCauseCategory.SOFT_DECLINE) -> DiagnosisResult:
    return DiagnosisResult(
        root_cause_category=category,
        specific_reason="test",
        confidence_score=0.9,
        reasoning="test"
    )

def test_policy_escalation_always_allowed():
    case = create_mock_domain_case(amount_paise=10000000) # High value
    result = evaluate_policy(case, create_mock_decision(RecoveryActionType.ESCALATE_TO_HUMAN), create_mock_diagnosis())
    assert result.allowed is True
    assert "always permitted" in result.reason

def test_policy_stop_always_allowed():
    case = create_mock_domain_case(amount_paise=500)
    result = evaluate_policy(case, create_mock_decision(RecoveryActionType.STOP), create_mock_diagnosis())
    assert result.allowed is True

def test_policy_high_value_blocks_financial_actions():
    # Value is 50,001 INR (5000100 paise) which is above the 50,000 threshold
    case = create_mock_domain_case(amount_paise=PolicyConfig.REQUIRE_HUMAN_APPROVAL_ABOVE_PAISE + 100)
    
    # Financial action (Retry) should be blocked
    result = evaluate_policy(case, create_mock_decision(RecoveryActionType.RETRY_CHARGE, {"delay_hours": 24}), create_mock_diagnosis())
    assert result.allowed is False
    assert "exceeds automatic threshold" in result.reason
    
    # Non-financial action (Reminder) should still be allowed
    result = evaluate_policy(case, create_mock_decision(RecoveryActionType.SEND_REMINDER, {"channel": "email"}), create_mock_diagnosis())
    assert result.allowed is True

def test_policy_discount_caps():
    case = create_mock_domain_case(amount_paise=100000) # 1000 INR
    
    # Within cap
    result = evaluate_policy(case, create_mock_decision(RecoveryActionType.OFFER_DISCOUNT, {"discount_percent": 15}), create_mock_diagnosis())
    assert result.allowed is True
    
    # Above cap
    result = evaluate_policy(case, create_mock_decision(RecoveryActionType.OFFER_DISCOUNT, {"discount_percent": 16}), create_mock_diagnosis())
    assert result.allowed is False
    assert "exceeds policy maximum" in result.reason
    
    # Cumulative above cap
    # 10% discount on 100000 paise = 10000 paise. 10000 existing + 10000 proposed = 20000. 
    # Max is 15% of 100000 = 15000. This should be blocked.
    case_with_discount = create_mock_domain_case(amount_paise=100000, cumulative_discount=10000)
    result = evaluate_policy(case_with_discount, create_mock_decision(RecoveryActionType.OFFER_DISCOUNT, {"discount_percent": 10}), create_mock_diagnosis())
    assert result.allowed is False
    assert "Cumulative discount exceeds policy maximum" in result.reason
    
    # Zero or negative
    try:
        bad_decision = create_mock_decision(RecoveryActionType.OFFER_DISCOUNT, {"discount_percent": 0})
        result = evaluate_policy(case, bad_decision, create_mock_diagnosis())
        assert result.allowed is False
        assert "greater than zero" in result.reason
    except ValueError:
        pass

def test_policy_webhook_hard_decline_blocked():
    """
    Proves that Rule 4 blocks hard declines based on the AI diagnosis,
    even when the webhook payload doesn't contain a top-level 'reason' string.
    """
    # A real Razorpay webhook payload shape:
    webhook_payload = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_reason": "lost_card"
                }
            }
        }
    }
    hard_case = create_mock_domain_case(amount_paise=100000, raw_signal_payload=webhook_payload)
    
    # AI accurately diagnoses this as a hard decline:
    hard_diag = create_mock_diagnosis(RootCauseCategory.HARD_DECLINE)
    
    # Rule 4 should correctly block it:
    result = evaluate_policy(hard_case, create_mock_decision(RecoveryActionType.RETRY_CHARGE, {"delay_hours": 24}), hard_diag)
    assert result.allowed is False
    assert "forbids retrying hard declines" in result.reason

    # Soft decline should be allowed:
    soft_diag = create_mock_diagnosis(RootCauseCategory.SOFT_DECLINE)
    result = evaluate_policy(hard_case, create_mock_decision(RecoveryActionType.RETRY_CHARGE, {"delay_hours": 24}), soft_diag)
    assert result.allowed is True

def test_policy_max_retries_capped():
    # Under limit (allowed)
    case_allowed = create_mock_domain_case(amount_paise=100000, retry_count=PolicyConfig.MAX_RETRIES - 1)
    result = evaluate_policy(case_allowed, create_mock_decision(RecoveryActionType.RETRY_CHARGE, {"delay_hours": 24}), create_mock_diagnosis())
    assert result.allowed is True
    
    # At limit (blocked because retry_count >= MAX_RETRIES)
    case_blocked = create_mock_domain_case(amount_paise=100000, retry_count=PolicyConfig.MAX_RETRIES)
    result = evaluate_policy(case_blocked, create_mock_decision(RecoveryActionType.RETRY_CHARGE, {"delay_hours": 24}), create_mock_diagnosis())
    assert result.allowed is False
    assert "Max retries" in result.reason

def test_get_policy_endpoint():
    client = TestClient(app)
    response = client.get("/api/v1/policy")
    assert response.status_code == 200
    data = response.json()
    assert data["max_retries"] == PolicyConfig.MAX_RETRIES
    assert data["max_discount_percent"] == PolicyConfig.MAX_DISCOUNT_PERCENT
    assert data["require_human_approval_above_paise"] == PolicyConfig.REQUIRE_HUMAN_APPROVAL_ABOVE_PAISE
    assert data["block_hard_declines"] == PolicyConfig.BLOCK_HARD_DECLINES

if __name__ == "__main__":
    test_policy_escalation_always_allowed()
    test_policy_stop_always_allowed()
    test_policy_high_value_blocks_financial_actions()
    test_policy_discount_caps()
    test_policy_webhook_hard_decline_blocked()
    test_policy_max_retries_capped()
    test_get_policy_endpoint()
    print("SUCCESS: All policy tests passed!")
