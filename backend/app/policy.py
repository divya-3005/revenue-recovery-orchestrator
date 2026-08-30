"""
Guardrail & Policy System (Feature 3 + Feature 8).

Deterministic rules the AI decision engine must operate inside.
Every rule is visible and inspectable — not buried in code.
"""

from app.models import (
    RecoveryCase, DecisionResult, DiagnosisResult, PolicyResult,
    RecoveryActionType, RootCauseCategory,
)

# ── Policy Configuration (visible, inspectable — Feature 3) ─────────────

POLICY = {
    "max_retries": 3,
    "max_discount_percent": 15,
    "require_human_approval_above_paise": 5_000_000,  # ₹50,000
    "block_hard_declines": True,
    "min_confidence_score": 0.7,
    "pre_debit_notice_hours": 72,  # RBI eNACH/NACH minimum
    "max_days_pursued": 14,
}


def evaluate_policy(
    case: RecoveryCase,
    decision: DecisionResult,
    diagnosis: DiagnosisResult,
) -> PolicyResult:
    """
    Check the AI's proposed action against every guardrail.
    Returns PolicyResult with allowed=True/False and the reason.
    """
    action = decision.recommended_action
    params = decision.action_parameters
    rules = []

    def reject(reason, human=False):
        return PolicyResult(
            allowed=False, reason=reason, requires_human_approval=human,
            decision=decision, rules_checked=rules,
        )

    def approve(reason):
        return PolicyResult(
            allowed=True, reason=reason, decision=decision, rules_checked=rules,
        )

    # Rule 0: Customer opted out — hard block everything
    if case.opted_out:
        rules.append({"rule": "customer_opt_out", "passed": False})
        return reject("Blocked: customer opted out of recovery communications.")

    rules.append({"rule": "customer_opt_out", "passed": True})

    # Rule 1: Escalation and stop are always allowed
    if action in (RecoveryActionType.ESCALATE_TO_HUMAN, RecoveryActionType.STOP):
        rules.append({"rule": "always_allowed", "passed": True})
        return approve(f"{action.value} is always permitted.")

    # Rule 2: Disputes require human review
    if diagnosis.root_cause_category == RootCauseCategory.DISPUTE:
        rules.append({"rule": "dispute_gate", "passed": False})
        return reject("Blocked: dispute detected — requires human intervention.", human=True)

    rules.append({"rule": "dispute_gate", "passed": True})

    # Rule 3: High-value cases need human approval for financial actions
    if case.amount_paise > POLICY["require_human_approval_above_paise"]:
        if action != RecoveryActionType.SEND_REMINDER:
            rules.append({"rule": "high_value_gate", "passed": False, "amount": case.amount_paise})
            return reject(
                f"Blocked: case value ({case.amount_paise} paise) exceeds ₹50,000 threshold. Human approval required.",
                human=True,
            )

    rules.append({"rule": "high_value_gate", "passed": True})

    # Rule 4: Cap cumulative discount
    if action == RecoveryActionType.OFFER_DISCOUNT:
        pct = params.get("discount_percent", 0)
        proposed_paise = int(case.amount_paise * pct / 100)
        max_paise = int(case.amount_paise * POLICY["max_discount_percent"] / 100)
        if case.cumulative_discount_paise + proposed_paise > max_paise:
            rules.append({"rule": "discount_cap", "passed": False})
            return reject(f"Blocked: cumulative discount would exceed {POLICY['max_discount_percent']}%.")

    rules.append({"rule": "discount_cap", "passed": True})

    # Rule 5: Never retry a hard decline
    financial_actions = (
        RecoveryActionType.RETRY_CHARGE,
        RecoveryActionType.CREATE_PAYMENT_LINK,
        RecoveryActionType.SWITCH_RAIL,
    )
    if action in financial_actions and diagnosis.root_cause_category == RootCauseCategory.HARD_DECLINE:
        rules.append({"rule": "block_hard_decline", "passed": False})
        return reject("Blocked: policy forbids retrying hard declines.")

    rules.append({"rule": "block_hard_decline", "passed": True})

    # Rule 6: Max retry attempts
    # NOTE: max_attempts in pipeline.py is POLICY["max_retries"] + 1 ("initial
    # try + retries"). retry_count is incremented AFTER each failed attempt,
    # so on the loop's final allowed iteration retry_count == max_retries
    # exactly — that attempt must still be allowed to execute. Using >= here
    # blocks it one iteration early, short-circuiting the case to FAILED via
    # a policy rejection that never calls the executor, instead of letting it
    # exhaust its full attempt budget and reach ESCALATED (Feature 9).
    if action in financial_actions and case.retry_count > POLICY["max_retries"]:
        rules.append({"rule": "max_retries", "passed": False})
        return reject(f"Blocked: max retries ({POLICY['max_retries']}) reached.")

    rules.append({"rule": "max_retries", "passed": True})

    # Rule 7: Minimum AI confidence
    if decision.confidence_score < POLICY["min_confidence_score"] or \
       diagnosis.confidence_score < POLICY["min_confidence_score"]:
        rules.append({"rule": "min_confidence", "passed": False})
        return reject(
            f"Blocked: AI confidence too low (decision={decision.confidence_score}, "
            f"diagnosis={diagnosis.confidence_score}). Escalating.",
            human=True,
        )

    rules.append({"rule": "min_confidence", "passed": True})

    # Rule 8: RBI pre-debit notice for eNACH/NACH mandates
    if action in financial_actions:
        rail = params.get("target_rail", case.payment_rail or "").lower()
        if rail in ("enach", "nach", "emandate", "mandate"):
            delay = params.get("delay_hours", 0)
            if delay < POLICY["pre_debit_notice_hours"]:
                rules.append({"rule": "rbi_pre_debit", "passed": False})
                return reject(
                    f"Blocked: RBI requires {POLICY['pre_debit_notice_hours']}h pre-debit notice "
                    f"for {rail.upper()}. Proposed delay: {delay}h.",
                )

    rules.append({"rule": "rbi_pre_debit", "passed": True})

    return approve("Action allowed by policy.")
