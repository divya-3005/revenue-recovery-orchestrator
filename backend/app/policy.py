from app.domain import (
    RecoveryCaseContext, RecoveryActionType, DecisionResult, 
    PolicyApprovedDecision, PolicyEvaluationResult, DiagnosisResult, RootCauseCategory
)
import hashlib

class PolicyConfig:
    MAX_RETRIES: int = 3
    MAX_DISCOUNT_PERCENT: int = 15
    # Value above which automatic financial actions are blocked (50,000 INR = 5000000 paise)
    REQUIRE_HUMAN_APPROVAL_ABOVE_PAISE: int = 5000000 
    BLOCK_HARD_DECLINES: bool = True
    MIN_CONFIDENCE_SCORE: float = 0.7

def _generate_idempotency_key(case: RecoveryCaseContext, decision: DecisionResult) -> str:
    """Generates a deterministic idempotency key for the approved action."""
    raw_key = f"{case.id}-{decision.recommended_action.value}-{case.retry_count}"
    return hashlib.sha256(raw_key.encode('utf-8')).hexdigest()

def evaluate_policy(case: RecoveryCaseContext, decision: DecisionResult, diagnosis: DiagnosisResult) -> PolicyEvaluationResult:
    """
    Deterministic rule layer that gates the AI decision engine.
    Returns a PolicyEvaluationResult containing the PolicyApprovedDecision if allowed.
    """
    proposed_action = decision.recommended_action
    proposed_parameters = decision.action_parameters

    def reject(reason: str, requires_human_approval: bool = False) -> PolicyEvaluationResult:
        return PolicyEvaluationResult(allowed=False, reason=reason, approved_decision=None, requires_human_approval=requires_human_approval)

    def approve(reason: str) -> PolicyEvaluationResult:
        approved = PolicyApprovedDecision(
            decision=decision,
            policy_reason=reason,
            idempotency_key=_generate_idempotency_key(case, decision)
        )
        return PolicyEvaluationResult(allowed=True, reason=reason, approved_decision=approved, requires_human_approval=False)

    # Rule 1: Escalation and stopping are always allowed
    if proposed_action in [RecoveryActionType.ESCALATE_TO_HUMAN, RecoveryActionType.STOP]:
        return approve(f"Action {proposed_action.value} is always permitted.")

    # Rule 2: High value cases require human approval for financial actions
    if case.amount_paise > PolicyConfig.REQUIRE_HUMAN_APPROVAL_ABOVE_PAISE:
        if proposed_action != RecoveryActionType.SEND_REMINDER: 
            return reject(f"Action blocked: Case value ({case.amount_paise} paise) exceeds automatic threshold. Human approval required.", requires_human_approval=True)

    # Rule 3: Cap discount percentage cumulatively
    if proposed_action == RecoveryActionType.OFFER_DISCOUNT:
        discount_pct = proposed_parameters.get("discount_percent", 0)
        if discount_pct <= 0:
            return reject("Action blocked: Discount percentage must be greater than zero.")
            
        proposed_discount_paise = case.amount_paise * (discount_pct / 100)
        max_discount_paise = case.amount_paise * (PolicyConfig.MAX_DISCOUNT_PERCENT / 100)
        
        if case.cumulative_discount_paise + proposed_discount_paise > max_discount_paise:
            return reject(f"Action blocked: Cumulative discount exceeds policy maximum ({PolicyConfig.MAX_DISCOUNT_PERCENT}%).")

    # Rule 4: Block retries on hard declines
    if proposed_action == RecoveryActionType.RETRY_CHARGE and PolicyConfig.BLOCK_HARD_DECLINES:
        if diagnosis.root_cause_category == RootCauseCategory.HARD_DECLINE:
            return reject("Action blocked: Policy forbids retrying hard declines.")

    # Rule 5: Max retries cap
    if proposed_action == RecoveryActionType.RETRY_CHARGE:
        if case.retry_count >= PolicyConfig.MAX_RETRIES:
            return reject(f"Action blocked: Max retries ({PolicyConfig.MAX_RETRIES}) reached.")

    # Rule 6: Confidence score minimum threshold
    if decision.confidence_score < PolicyConfig.MIN_CONFIDENCE_SCORE or diagnosis.confidence_score < PolicyConfig.MIN_CONFIDENCE_SCORE:
        return reject(
            f"Action blocked: AI confidence too low (Decision: {decision.confidence_score}, Diagnosis: {diagnosis.confidence_score}). Escalating to human.", 
            requires_human_approval=True
        )

    return approve("Action allowed by policy.")
