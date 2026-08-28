from app.ai.diagnosis import diagnose_case
from app.ai.provider import AIProvider
from app.domain import RecoveryCaseContext, DiagnosisResult, RootCauseCategory
from app.models import CaseType, CaseStatus
import json

class MockStrictProvider(AIProvider):
    def __init__(self, expected_category: RootCauseCategory, expected_confidence: float):
        self.expected_category = expected_category
        self.expected_confidence = expected_confidence
        self.last_prompt = ""

    def ask_structured(self, prompt: str, response_schema):
        self.last_prompt = prompt
        return response_schema(
            root_cause_category=self.expected_category,
            specific_reason="mock_reason",
            confidence_score=self.expected_confidence,
            reasoning="Mock reasoning grounded in prompt data."
        )

def get_base_case(payload: dict) -> RecoveryCaseContext:
    return RecoveryCaseContext(
        id="case_test_123",
        case_type=CaseType.SUBSCRIPTION_FAILED,
        status=CaseStatus.OPEN,
        amount_paise=250000,
        currency="INR",
        customer_id="cust_test",
        raw_signal_payload=payload,
        retry_count=0,
        cumulative_discount_paise=0
    )

def test_diagnose_soft_decline_payload():
    payload = {
        "error_code": "BAD_REQUEST_ERROR", 
        "error_description": "Insufficient funds in customer account", 
        "error_source": "issuing_bank"
    }
    case = get_base_case(payload)
    provider = MockStrictProvider(RootCauseCategory.SOFT_DECLINE, 0.95)
    
    result = diagnose_case(case, provider)
    
    assert result.root_cause_category == RootCauseCategory.SOFT_DECLINE
    assert result.confidence_score == 0.95
    assert "Insufficient funds in customer account" in provider.last_prompt

def test_diagnose_hard_decline_payload():
    payload = {
        "error_code": "BAD_REQUEST_ERROR", 
        "error_description": "Card reported as lost or stolen", 
        "error_source": "issuing_bank"
    }
    case = get_base_case(payload)
    provider = MockStrictProvider(RootCauseCategory.HARD_DECLINE, 0.99)
    
    result = diagnose_case(case, provider)
    
    assert result.root_cause_category == RootCauseCategory.HARD_DECLINE
    assert result.confidence_score == 0.99
    assert "lost or stolen" in provider.last_prompt

def test_diagnose_ambiguous_payload():
    payload = {}  # Empty payload gives no evidence
    case = get_base_case(payload)
    provider = MockStrictProvider(RootCauseCategory.UNKNOWN, 0.20)
    
    result = diagnose_case(case, provider)
    
    assert result.root_cause_category == RootCauseCategory.UNKNOWN
    assert result.confidence_score == 0.20
    assert "{}" in provider.last_prompt

if __name__ == "__main__":
    test_diagnose_soft_decline_payload()
    test_diagnose_hard_decline_payload()
    test_diagnose_ambiguous_payload()
    print("SUCCESS: Diagnosis tests passed.")
