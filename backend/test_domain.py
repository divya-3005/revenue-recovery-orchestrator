from app.domain import RecoveryCaseContext, DiagnosisResult, RootCauseCategory
from app.models import RecoveryCase, CaseType, CaseStatus
from pydantic import ValidationError

def test_orm_to_domain_mapping():
    # 1. Create a raw ORM object with JSON diagnosis
    orm_case = RecoveryCase(
        id="test-123",
        case_type=CaseType.CHECKOUT_ABANDONED,
        status=CaseStatus.OPEN,
        amount_paise=50000,
        currency="INR",
        customer_id="cust-1",
        raw_signal_payload={"some": "data"},
        retry_count=2,
        cumulative_discount_paise=1000,
        active_diagnosis={
            "root_cause_category": "friction",
            "specific_reason": "high_cart_value",
            "confidence_score": 0.85,
            "reasoning": "User dropped off at payment step with high value cart."
        }
    )

    # 2. Convert to pure Domain object
    domain_case = RecoveryCaseContext.model_validate(orm_case)

    # 3. Verify conversion
    assert domain_case.id == "test-123"
    assert domain_case.retry_count == 2
    assert domain_case.active_diagnosis is not None
    assert domain_case.active_diagnosis.root_cause_category == RootCauseCategory.FRICTION
    assert domain_case.active_diagnosis.confidence_score == 0.85

def test_diagnosis_confidence_score_validation():
    # Valid
    valid_diag = DiagnosisResult(
        root_cause_category=RootCauseCategory.HARD_DECLINE,
        specific_reason="stolen_card",
        confidence_score=1.0,
        reasoning="Bank returned stolen card code."
    )
    assert valid_diag.confidence_score == 1.0

    # Invalid - Too high
    try:
        DiagnosisResult(
            root_cause_category=RootCauseCategory.HARD_DECLINE,
            specific_reason="stolen_card",
            confidence_score=1.1,
            reasoning="Bank returned stolen card code."
        )
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass

    # Invalid - Too low
    try:
        DiagnosisResult(
            root_cause_category=RootCauseCategory.HARD_DECLINE,
            specific_reason="stolen_card",
            confidence_score=-0.1,
            reasoning="Bank returned stolen card code."
        )
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass

if __name__ == "__main__":
    test_orm_to_domain_mapping()
    test_diagnosis_confidence_score_validation()
    print("SUCCESS: Domain mapping and validation tests passed.")
