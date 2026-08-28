from app.policy import evaluate_policy, RecoveryActionType, PolicyConfig
from fastapi.testclient import TestClient
from app.main import app

class MockCase:
    def __init__(self, amount_paise, raw_signal_payload=None):
        self.amount_paise = amount_paise
        self.raw_signal_payload = raw_signal_payload

def test_policy_escalation_always_allowed():
    case = MockCase(amount_paise=10000000) # High value
    is_allowed, reason = evaluate_policy(case, RecoveryActionType.ESCALATE_TO_HUMAN)
    assert is_allowed is True
    assert "always permitted" in reason

def test_policy_stop_always_allowed():
    case = MockCase(amount_paise=500)
    is_allowed, reason = evaluate_policy(case, RecoveryActionType.STOP)
    assert is_allowed is True

def test_policy_high_value_blocks_financial_actions():
    # Value is 50,001 INR (5000100 paise) which is above the 50,000 threshold
    case = MockCase(amount_paise=PolicyConfig.REQUIRE_HUMAN_APPROVAL_ABOVE_PAISE + 100)
    
    # Financial action (Retry) should be blocked
    is_allowed, reason = evaluate_policy(case, RecoveryActionType.RETRY_CHARGE)
    assert is_allowed is False
    assert "exceeds automatic threshold" in reason
    
    # Non-financial action (Reminder) should still be allowed
    is_allowed, reason = evaluate_policy(case, RecoveryActionType.SEND_REMINDER)
    assert is_allowed is True

def test_policy_discount_caps():
    case = MockCase(amount_paise=100000) # 1000 INR
    
    # Within cap
    is_allowed, reason = evaluate_policy(case, RecoveryActionType.OFFER_DISCOUNT, {"discount_percent": 15})
    assert is_allowed is True
    
    # Above cap
    is_allowed, reason = evaluate_policy(case, RecoveryActionType.OFFER_DISCOUNT, {"discount_percent": 16})
    assert is_allowed is False
    assert "exceeds policy maximum" in reason
    
    # Zero or negative
    is_allowed, reason = evaluate_policy(case, RecoveryActionType.OFFER_DISCOUNT, {"discount_percent": 0})
    assert is_allowed is False
    assert "greater than zero" in reason

def test_policy_hard_declines_blocked():
    # Soft decline
    soft_case = MockCase(amount_paise=100000, raw_signal_payload={"reason": "insufficient_funds"})
    is_allowed, _ = evaluate_policy(soft_case, RecoveryActionType.RETRY_CHARGE)
    assert is_allowed is True
    
    # Hard decline
    hard_case = MockCase(amount_paise=100000, raw_signal_payload={"reason": "lost_card_reported"})
    is_allowed, reason = evaluate_policy(hard_case, RecoveryActionType.RETRY_CHARGE)
    assert is_allowed is False
    assert "forbids retrying hard declines" in reason

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
    test_policy_hard_declines_blocked()
    test_get_policy_endpoint()
    print("SUCCESS: All policy tests passed!")
