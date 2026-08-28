from typing import Optional, Type, Any
from app.orchestrator import process_case
from app.ai.provider import AIProvider
from app.execution.executor import ActionExecutor
from app.domain import (
    RecoveryCaseContext, RootCauseCategory, RecoveryActionType, 
    DiagnosisResult, DecisionResult, ExecutionResult, ExecutionStatus,
    PolicyApprovedDecision
)
from app.models import CaseType, CaseStatus

class MockPipelineProvider(AIProvider):
    def __init__(self, diagnosis: DiagnosisResult, decision: DecisionResult):
        self.diagnosis = diagnosis
        self.decision = decision

    def ask_structured(self, prompt: str, response_schema: Type[Any]) -> Any:
        if response_schema == DiagnosisResult:
            return self.diagnosis
        elif response_schema == DecisionResult:
            return self.decision
        raise ValueError("Unknown schema requested")

class SpyDryRunExecutor(ActionExecutor):
    def __init__(self):
        self.call_count = 0
        self.last_case = None
        self.last_approved_decision = None

    def execute(self, case: RecoveryCaseContext, approved_decision: PolicyApprovedDecision) -> ExecutionResult:
        if not isinstance(approved_decision, PolicyApprovedDecision):
            raise TypeError("ActionExecutor requires a PolicyApprovedDecision.")
            
        self.call_count += 1
        self.last_case = case
        self.last_approved_decision = approved_decision
        
        return ExecutionResult(
            status=ExecutionStatus.DRY_RUN,
            action_taken=approved_decision.decision.recommended_action,
            reason="Spy dry run",
            action_parameters_used=approved_decision.decision.action_parameters
        )

def get_base_case(payload=None, amount=100000) -> RecoveryCaseContext:
    return RecoveryCaseContext(
        id="case_orch_123",
        case_type=CaseType.CHECKOUT_ABANDONED,
        status=CaseStatus.OPEN,
        amount_paise=amount,
        currency="INR",
        customer_id="cust_1",
        raw_signal_payload=payload or {},
        retry_count=0,
        cumulative_discount_paise=0
    )

def test_safe_successful_path():
    case = get_base_case({"reason": "insufficient_funds"})
    
    # 1. AI Output Mocks
    diagnosis = DiagnosisResult(
        root_cause_category=RootCauseCategory.SOFT_DECLINE,
        specific_reason="insufficient_funds",
        confidence_score=0.9,
        reasoning="Test"
    )
    decision = DecisionResult(
        recommended_action=RecoveryActionType.RETRY_CHARGE,
        action_parameters={"delay_hours": 24},
        confidence_score=0.9,
        reasoning="Test"
    )
    
    provider = MockPipelineProvider(diagnosis, decision)
    executor = SpyDryRunExecutor()
    
    # 2. Process pipeline
    result = process_case(case, provider, executor)
    
    # 3. Assertions
    assert result.diagnosis is not None
    assert result.decision is not None
    assert result.policy_evaluation.allowed is True
    assert result.policy_evaluation.approved_decision is not None
    
    assert executor.call_count == 1
    assert result.execution_result.status == ExecutionStatus.DRY_RUN

def test_ai_hallucination_is_blocked():
    case = get_base_case()
    
    # 1. AI Output Mocks (Hallucinates 50% discount)
    diagnosis = DiagnosisResult(
        root_cause_category=RootCauseCategory.FRICTION,
        specific_reason="drop_off",
        confidence_score=0.9,
        reasoning="Test"
    )
    decision = DecisionResult(
        recommended_action=RecoveryActionType.OFFER_DISCOUNT,
        action_parameters={"discount_percent": 50},
        confidence_score=0.9,
        reasoning="Test"
    )
    
    provider = MockPipelineProvider(diagnosis, decision)
    executor = SpyDryRunExecutor()
    
    # 2. Process pipeline
    result = process_case(case, provider, executor)
    
    # 3. Assertions
    assert result.decision.recommended_action == RecoveryActionType.OFFER_DISCOUNT
    assert result.policy_evaluation.allowed is False
    assert result.policy_evaluation.approved_decision is None
    
    # EXECUTOR MUST NEVER BE CALLED
    assert executor.call_count == 0
    assert result.execution_result is None

def test_hard_decline_retry_blocked():
    case = get_base_case({"reason": "lost_card"})
    
    # 1. AI Output Mocks (Proposes retry on hard decline)
    diagnosis = DiagnosisResult(
        root_cause_category=RootCauseCategory.HARD_DECLINE,
        specific_reason="lost_card",
        confidence_score=0.99,
        reasoning="Test"
    )
    decision = DecisionResult(
        recommended_action=RecoveryActionType.RETRY_CHARGE,
        action_parameters={"delay_hours": 24},
        confidence_score=0.9,
        reasoning="Test"
    )
    
    provider = MockPipelineProvider(diagnosis, decision)
    executor = SpyDryRunExecutor()
    
    # 2. Process pipeline
    result = process_case(case, provider, executor)
    
    # 3. Assertions
    assert result.policy_evaluation.allowed is False
    assert executor.call_count == 0
    assert result.execution_result is None

def test_unknown_diagnosis_escalation():
    case = get_base_case()
    
    # 1. AI Output Mocks
    diagnosis = DiagnosisResult(
        root_cause_category=RootCauseCategory.UNKNOWN,
        specific_reason="no data",
        confidence_score=0.1,
        reasoning="Test"
    )
    decision = DecisionResult(
        recommended_action=RecoveryActionType.ESCALATE_TO_HUMAN,
        action_parameters={},
        confidence_score=0.9,
        reasoning="Test"
    )
    
    provider = MockPipelineProvider(diagnosis, decision)
    executor = SpyDryRunExecutor()
    
    # 2. Process pipeline
    result = process_case(case, provider, executor)
    
    # 3. Assertions
    assert result.policy_evaluation.allowed is True
    assert result.policy_evaluation.approved_decision is not None
    assert executor.call_count == 1
    assert result.execution_result.status == ExecutionStatus.DRY_RUN

def test_executor_boundary():
    case = get_base_case()
    raw_decision = DecisionResult(
        recommended_action=RecoveryActionType.SEND_REMINDER,
        action_parameters={"channel": "email"},
        confidence_score=0.9,
        reasoning="Test"
    )
    
    executor = SpyDryRunExecutor()
    
    try:
        executor.execute(case, raw_decision) # type: ignore
        assert False, "Executor allowed a raw, unapproved DecisionResult"
    except TypeError:
        pass

if __name__ == "__main__":
    test_safe_successful_path()
    test_ai_hallucination_is_blocked()
    test_hard_decline_retry_blocked()
    test_unknown_diagnosis_escalation()
    test_executor_boundary()
    print("SUCCESS: Orchestrator pipeline tests passed.")
