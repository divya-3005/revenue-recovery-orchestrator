from app.domain import RecoveryCaseContext, PipelineResult
from app.ai.provider import AIProvider
from app.execution.executor import ActionExecutor
from app.ai.diagnosis import diagnose_case
from app.ai.decision import decide_action
from app.policy import evaluate_policy

def process_case(
    case: RecoveryCaseContext, 
    provider: AIProvider, 
    executor: ActionExecutor
) -> PipelineResult:
    """
    Orchestrates the purely domain-level pipeline:
    Diagnose -> Decide -> Evaluate Policy -> Execute
    """
    # 1. Diagnose the case
    diagnosis = diagnose_case(case, provider)
    
    # 2. Decide on an action
    decision = decide_action(case, diagnosis, provider)
    
    # 3. Deterministic policy evaluation
    policy_evaluation = evaluate_policy(case, decision)
    
    # 4. Safe execution boundary
    execution_result = None
    if policy_evaluation.allowed:
        # Guarantee we are passing the approved wrapper, not the raw decision
        execution_result = executor.execute(case, policy_evaluation.approved_decision)
        
    # 5. Return complete state
    return PipelineResult(
        diagnosis=diagnosis,
        decision=decision,
        policy_evaluation=policy_evaluation,
        execution_result=execution_result
    )
