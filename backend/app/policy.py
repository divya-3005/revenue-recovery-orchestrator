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
    # RBI mandate: eNACH/NACH charges require minimum 3 business-day (72h) pre-debit notification
    MIN_ENACH_DELAY_HOURS: int = 72
    PRE_DEBIT_NOTICE_HOURS: int = 24
    MAX_DAYS_PURSUED: int = 14

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

    rules = []

    def reject(reason: str, requires_human_approval: bool = False) -> PolicyEvaluationResult:
        return PolicyEvaluationResult(allowed=False, reason=reason, approved_decision=None, requires_human_approval=requires_human_approval, rules=rules)

    def approve(reason: str) -> PolicyEvaluationResult:
        approved = PolicyApprovedDecision(
            decision=decision,
            policy_reason=reason,
            idempotency_key=_generate_idempotency_key(case, decision)
        )
        return PolicyEvaluationResult(allowed=True, reason=reason, approved_decision=approved, requires_human_approval=False, rules=rules)

    # Rule 0: Customer opt-out — immediate hard block on all actions
    if case.opted_out:
        rules.append({"rule_name": "customer_opt_out", "name": "customer_opt_out", "passed": False})
        return reject("Action blocked: Customer has opted out of recovery communications.")
    rules.append({"rule_name": "customer_opt_out", "name": "customer_opt_out", "passed": True})

    # Rule 1: Escalation and stopping are always allowed
    if proposed_action in [RecoveryActionType.ESCALATE_TO_HUMAN, RecoveryActionType.STOP]:
        rules.append({"rule_name": "always_allowed", "name": "always_allowed", "passed": True, "action": proposed_action.value})
        return approve(f"Action {proposed_action.value} is always permitted.")

    # Rule 1b: Deterministic dispute gate — disputes require human review
    if diagnosis.root_cause_category == RootCauseCategory.DISPUTE:
        rules.append({"rule_name": "dispute_human_gate", "name": "dispute_human_gate", "passed": False})
        return reject("Action blocked: Customer dispute detected. Immediate human intervention required.", requires_human_approval=True)
    rules.append({"rule_name": "dispute_human_gate", "name": "dispute_human_gate", "passed": True})

    # Rule 2: High value cases require human approval for financial actions
    if case.amount_paise > PolicyConfig.REQUIRE_HUMAN_APPROVAL_ABOVE_PAISE:
        if proposed_action != RecoveryActionType.SEND_REMINDER: 
            rules.append({"rule_name": "high_value_financial", "name": "high_value_financial", "passed": False, "amount": case.amount_paise})
            return reject(f"Action blocked: Case value ({case.amount_paise} paise) exceeds automatic threshold. Human approval required.", requires_human_approval=True)
        else:
            rules.append({"rule_name": "high_value_financial", "name": "high_value_financial", "passed": True, "amount": case.amount_paise})
    else:
        rules.append({"rule_name": "high_value_financial", "name": "high_value_financial", "passed": True, "amount": case.amount_paise})

    # Rule 3: Cap discount percentage cumulatively
    if proposed_action == RecoveryActionType.OFFER_DISCOUNT:
        discount_pct = proposed_parameters.get("discount_percent", 0)
        if discount_pct <= 0:
            rules.append({"rule_name": "discount_cap", "name": "discount_cap", "passed": False, "discount_pct": discount_pct})
            return reject("Action blocked: Discount percentage must be greater than zero.")
            
        proposed_discount_paise = case.amount_paise * (discount_pct / 100)
        max_discount_paise = case.amount_paise * (PolicyConfig.MAX_DISCOUNT_PERCENT / 100)
        
        if case.cumulative_discount_paise + proposed_discount_paise > max_discount_paise:
            rules.append({"rule_name": "discount_cap", "name": "discount_cap", "passed": False, "cumulative": case.cumulative_discount_paise, "proposed": proposed_discount_paise, "limit": max_discount_paise})
            return reject(f"Action blocked: Cumulative discount exceeds policy maximum ({PolicyConfig.MAX_DISCOUNT_PERCENT}%).")
        rules.append({"rule_name": "discount_cap", "name": "discount_cap", "passed": True})

    # Rule 4: Block retries, payment links, and rail switches on hard declines
    if proposed_action in (RecoveryActionType.RETRY_CHARGE, RecoveryActionType.CREATE_PAYMENT_LINK, RecoveryActionType.SWITCH_RAIL) and PolicyConfig.BLOCK_HARD_DECLINES:
        if diagnosis.root_cause_category == RootCauseCategory.HARD_DECLINE:
            rules.append({"rule_name": "block_hard_declines", "name": "block_hard_declines", "passed": False})
            return reject("Action blocked: Policy forbids retrying hard declines.")
        rules.append({"rule_name": "block_hard_declines", "name": "block_hard_declines", "passed": True})

    # Rule 5: Max retries cap
    if proposed_action in (RecoveryActionType.RETRY_CHARGE, RecoveryActionType.CREATE_PAYMENT_LINK, RecoveryActionType.SWITCH_RAIL):
        if case.retry_count >= PolicyConfig.MAX_RETRIES:
            rules.append({"rule_name": "max_retries", "name": "max_retries", "passed": False, "observed": case.retry_count, "limit": PolicyConfig.MAX_RETRIES})
            return reject(f"Action blocked: Max retries ({PolicyConfig.MAX_RETRIES}) reached.")
        rules.append({"rule_name": "max_retries", "name": "max_retries", "passed": True})

    # Rule 6: Confidence score minimum threshold
    if decision.confidence_score < PolicyConfig.MIN_CONFIDENCE_SCORE or diagnosis.confidence_score < PolicyConfig.MIN_CONFIDENCE_SCORE:
        rules.append({"rule_name": "min_confidence", "name": "min_confidence", "passed": False, "decision_score": decision.confidence_score, "diagnosis_score": diagnosis.confidence_score})
        return reject(
            f"Action blocked: AI confidence too low (Decision: {decision.confidence_score}, Diagnosis: {diagnosis.confidence_score}). Escalating to human.", 
            requires_human_approval=True
        )
    rules.append({"rule_name": "min_confidence", "name": "min_confidence", "passed": True})

    # Rule 7: RBI pre-debit notice — mandate charges require minimum pre-debit notice window (72h for eNACH/NACH)
    if proposed_action in (RecoveryActionType.RETRY_CHARGE, RecoveryActionType.CREATE_PAYMENT_LINK, RecoveryActionType.SWITCH_RAIL):
        # Bug 3 fix: For switch_rail, evaluate the target rail being moved TO, not the prior failed rail
        if proposed_action == RecoveryActionType.SWITCH_RAIL:
            effective_rail = (proposed_parameters.get("target_rail") or "").lower()
        else:
            effective_rail = (case.payment_rail or "").lower()

        if effective_rail in ("emandate", "enach", "nach", "mandate"):
            delay_hours = proposed_parameters.get("delay_hours", 0)
            required_notice_hours = PolicyConfig.MIN_ENACH_DELAY_HOURS
            if delay_hours < required_notice_hours:
                rules.append({
                    "rule_name": "rbi_pre_debit_notice",
                    "name": "rbi_pre_debit_notice",
                    "passed": False,
                    "delay_hours": delay_hours,
                    "required": required_notice_hours,
                    "effective_rail": effective_rail
                })
                return reject(
                    f"Action blocked: RBI mandate regulations require a minimum {required_notice_hours}h "
                    f"pre-debit notification for {effective_rail.upper()} charges. Proposed delay: {delay_hours}h."
                )
            rules.append({
                "rule_name": "rbi_pre_debit_notice",
                "name": "rbi_pre_debit_notice",
                "passed": True,
                "effective_rail": effective_rail
            })

    return approve("Action allowed by policy.")
