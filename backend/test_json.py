"""
Tests that all domain models serialize to valid JSON via model_dump().
Catches enum serialization issues that would break API responses or Inngest step returns.
"""

import json
from app.domain import (
    RecoveryCaseContext, DiagnosisResult, RootCauseCategory,
    DecisionResult, RecoveryActionType, PolicyEvaluationResult,
    CaseType, CaseStatus,
)


def test_case_context_serializable():
    case = RecoveryCaseContext(
        id="123", case_type=CaseType.CHECKOUT_ABANDONED, status=CaseStatus.OPEN,
        amount_paise=100, currency="INR", customer_id="c1",
        retry_count=0, cumulative_discount_paise=0, raw_signal_payload={}
    )
    result = json.dumps(case.model_dump())
    assert isinstance(result, str)


def test_diagnosis_result_serializable():
    diag = DiagnosisResult(
        root_cause_category=RootCauseCategory.HARD_DECLINE,
        specific_reason="a", confidence_score=0.9, reasoning="b"
    )
    result = json.dumps(diag.model_dump())
    assert isinstance(result, str)


def test_decision_result_serializable():
    dec = DecisionResult(
        recommended_action=RecoveryActionType.RETRY_CHARGE,
        action_parameters={"delay_hours": 1},
        confidence_score=0.9, reasoning="b"
    )
    result = json.dumps(dec.model_dump())
    assert isinstance(result, str)


def test_policy_evaluation_serializable():
    pol = PolicyEvaluationResult(allowed=True, reason="ok")
    result = json.dumps(pol.model_dump())
    assert isinstance(result, str)
