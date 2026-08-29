from app.domain import (
    RecoveryCaseContext, DecisionResult, RecoveryActionType, 
    PolicyApprovedDecision, ExecutionStatus, ExecutionResult,
    DiagnosisResult, RootCauseCategory
)
from app.models import CaseType, CaseStatus
from app.execution.executor import DryRunExecutor
from app.policy import evaluate_policy

def get_base_case() -> RecoveryCaseContext:
    return RecoveryCaseContext(
        id="case_exec_123",
        case_type=CaseType.CHECKOUT_ABANDONED,
        status=CaseStatus.OPEN,
        amount_paise=100000,
        currency="INR",
        customer_id="cust_1",
        raw_signal_payload={},
        retry_count=0,
        cumulative_discount_paise=0
    )

def get_valid_decision() -> DecisionResult:
    return DecisionResult(
        recommended_action=RecoveryActionType.SEND_REMINDER,
        action_parameters={"channel": "email"},
        confidence_score=0.9,
        reasoning="Test"
    )

def get_dummy_diagnosis() -> DiagnosisResult:
    return DiagnosisResult(
        root_cause_category=RootCauseCategory.SOFT_DECLINE,
        specific_reason="test",
        confidence_score=0.9,
        reasoning="test"
    )

def test_approved_decision_can_reach_executor():
    case = get_base_case()
    decision = get_valid_decision()
    
    policy_result = evaluate_policy(case, decision, get_dummy_diagnosis())
    assert policy_result.allowed is True
    assert policy_result.approved_decision is not None
    assert policy_result.approved_decision.idempotency_key is not None
    
    executor = DryRunExecutor()
    execution_result = executor.execute(case, policy_result.approved_decision)
    
    assert execution_result.status == ExecutionStatus.DRY_RUN
    assert execution_result.action_taken == RecoveryActionType.SEND_REMINDER
    assert "Dry run" in execution_result.reason

def test_raw_decision_cannot_reach_executor():
    case = get_base_case()
    raw_decision = get_valid_decision()
    
    executor = DryRunExecutor()
    
    # Must explicitly raise TypeError when given a raw DecisionResult instead of PolicyApprovedDecision
    try:
        executor.execute(case, raw_decision)
        assert False, "Executor allowed unapproved decision to execute"
    except TypeError as e:
        assert "requires a PolicyApprovedDecision" in str(e)

def test_policy_rejected_decision_has_no_approval_token():
    case = get_base_case()
    case.amount_paise = 10000000 # High value requires human approval
    
    # AI Hallucinates a retry
    decision = DecisionResult(
        recommended_action=RecoveryActionType.RETRY_CHARGE,
        action_parameters={"delay_hours": 24},
        confidence_score=0.9,
        reasoning="Test"
    )
    
    policy_result = evaluate_policy(case, decision, get_dummy_diagnosis())
    assert policy_result.allowed is False
    assert policy_result.approved_decision is None
    # Because approved_decision is None, it is physically impossible to pass it to executor.execute()
    # unless someone explicitly passes None, which would fail the isinstance check.

if __name__ == "__main__":
    test_approved_decision_can_reach_executor()
    test_raw_decision_cannot_reach_executor()
    test_policy_rejected_decision_has_no_approval_token()
    print("SUCCESS: Execution boundary tests passed.")
