from app.ai.provider import AIProvider, FallbackProvider, GeminiProvider, GroqProvider, AnthropicProvider
from app.ai.diagnosis import diagnose_case
from app.domain import DiagnosisResult, RootCauseCategory, RecoveryCaseContext
from app.models import CaseType, CaseStatus
from pydantic import ValidationError

class MockPrimaryProvider(AIProvider):
    def __init__(self, should_fail_api=False, should_return_malformed=False):
        self.should_fail_api = should_fail_api
        self.should_return_malformed = should_return_malformed
        self.calls = 0

    def ask_structured(self, prompt: str, response_schema):
        self.calls += 1
        if self.should_fail_api:
            # Raise a real SDK exception to trigger the fallback
            try:
                import groq
                import httpx
                raise groq.RateLimitError(
                    message="Mock primary provider rate limited!", 
                    response=httpx.Response(429, request=httpx.Request("GET", "http://test")), 
                    body=None
                )
            except ImportError:
                class DummyAPIError(Exception): pass
                raise DummyAPIError("Mock primary provider rate limited!")
                
        if self.should_return_malformed:
            # Simulate returning bad json that fails validation
            # E.g. passing a string for a float field
            return response_schema.model_validate({"root_cause_category": "hard_decline", "specific_reason": "test", "confidence_score": 1.5, "reasoning": "test"})
            
        return response_schema(
            root_cause_category=RootCauseCategory.SOFT_DECLINE,
            specific_reason="insufficient_funds",
            confidence_score=0.9,
            reasoning="Mock primary reasoning"
        )

class MockFallbackProvider(AIProvider):
    def __init__(self):
        self.calls = 0

    def ask_structured(self, prompt: str, response_schema):
        self.calls += 1
        return response_schema(
            root_cause_category=RootCauseCategory.HARD_DECLINE,
            specific_reason="stolen_card",
            confidence_score=0.99,
            reasoning="Mock fallback reasoning"
        )

def get_dummy_case():
    return RecoveryCaseContext(
        id="test-1",
        case_type=CaseType.SUBSCRIPTION_FAILED,
        status=CaseStatus.OPEN,
        amount_paise=1000,
        currency="INR",
        customer_id="cust-1",
        raw_signal_payload={"reason": "insufficient_funds"},
        retry_count=0,
        cumulative_discount_paise=0
    )

def test_diagnose_success():
    provider = FallbackProvider(MockPrimaryProvider(), MockFallbackProvider())
    result = diagnose_case(get_dummy_case(), provider)
    
    assert result.root_cause_category == RootCauseCategory.SOFT_DECLINE
    assert provider.primary.calls == 1
    assert provider.fallback.calls == 0

def test_diagnose_fallback_on_api_error():
    provider = FallbackProvider(
        MockPrimaryProvider(should_fail_api=True), 
        MockFallbackProvider()
    )
    result = diagnose_case(get_dummy_case(), provider)
    
    # Should get fallback's result
    assert result.root_cause_category == RootCauseCategory.HARD_DECLINE
    assert provider.primary.calls == 1
    assert provider.fallback.calls == 1

def test_diagnose_no_fallback_on_validation_error():
    provider = FallbackProvider(
        MockPrimaryProvider(should_return_malformed=True), 
        MockFallbackProvider()
    )
    
    # Validation error should bubble up, NOT trigger fallback
    try:
        diagnose_case(get_dummy_case(), provider)
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass
        
    assert provider.primary.calls == 1
    assert provider.fallback.calls == 0

def test_sdk_imports():
    """Verify SDKs are installed and providers enforce API key requirements."""
    import os
    from unittest.mock import patch

    # 1. Without API keys, providers should raise ValueError
    with patch.dict(os.environ, {}, clear=True):
        try:
            GeminiProvider()
            assert False, "GeminiProvider should require GEMINI_API_KEY"
        except ValueError:
            pass

        try:
            GroqProvider()
            assert False, "GroqProvider should require GROQ_API_KEY"
        except ValueError:
            pass

        try:
            AnthropicProvider()
            assert False, "AnthropicProvider should require ANTHROPIC_API_KEY"
        except ValueError:
            pass

    # 2. With API keys set, providers should instantiate successfully
    with patch.dict(os.environ, {"GEMINI_API_KEY": "test", "GROQ_API_KEY": "test", "ANTHROPIC_API_KEY": "test"}):
        try:
            GeminiProvider()
            GroqProvider()
            AnthropicProvider()
        except Exception as e:
            assert False, f"SDK initialization failed with keys set: {e}"

if __name__ == "__main__":
    test_diagnose_success()
    test_diagnose_fallback_on_api_error()
    test_diagnose_no_fallback_on_validation_error()
    test_sdk_imports()
    print("SUCCESS: AI provider and fallback tests passed.")
