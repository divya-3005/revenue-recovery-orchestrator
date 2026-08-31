"""
Guardrail & Policy System (Feature 3 + Feature 8).

Deterministic rules the AI decision engine must operate inside.
Every rule is visible and inspectable — not buried in code.
"""

from typing import Optional
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
    # Feature 7 (re-loop): how long a PAYMENT_PENDING case can sit unpaid
    # before it's re-engaged with a firmer message, and how many such
    # follow-up passes are allowed before it's escalated instead (this is
    # itself a stopping rule — see run_follow_up_check in pipeline.py).
    "follow_up_after_hours": 48,
    "max_follow_ups": 2,
}


def evaluate_policy(
    case: RecoveryCase,
    decision: DecisionResult,
    diagnosis: DiagnosisResult,
    human_approved: bool = False,
) -> PolicyResult:
    """
    Check the AI's proposed action against every guardrail.
    Evaluates all 8 rules and tags each with status: 'passed', 'failed', or 'not_applicable'.
    Returns PolicyResult with allowed=True/False and the reason.

    human_approved=True means a human has already reviewed and signed off
    on this specific decision, so it clears every gate that exists purely
    to get a human in the loop (dispute_gate, high_value_gate,
    min_confidence). It does NOT override hard caps like discount_cap or
    block_hard_decline — those are deterministic money constraints no
    human should be able to wave through, approval or not.
    """
    action = decision.recommended_action
    params = decision.action_parameters
    rules = []
    first_blocker: Optional[dict] = None

    def note(name, passed, **extra):
        nonlocal first_blocker
        entry = {"rule": name, "status": "passed" if passed else "failed", **extra}
        rules.append(entry)
        if not passed and first_blocker is None:
            first_blocker = entry

    def skip(name, why):
        rules.append({"rule": name, "status": "not_applicable", "why": why})

    # Rule 0: Customer opted out — hard block everything
    if case.opted_out:
        note("customer_opt_out", False, reason="Customer opted out")
    else:
        note("customer_opt_out", True)

    # Rule 1: Escalation and stop are always allowed
    if action in (RecoveryActionType.ESCALATE_TO_HUMAN, RecoveryActionType.STOP):
        note("always_allowed", True, action=action.value)
        # Always allowed overrides other rules
        if not case.opted_out:
            return PolicyResult(
                allowed=True,
                reason=f"{action.value} is always permitted.",
                decision=decision,
                rules_checked=rules,
            )
    else:
        skip("always_allowed", "Action is not an escalation or stop")

    # Rule 2: Disputes require human review
    if diagnosis.root_cause_category == RootCauseCategory.DISPUTE:
        if human_approved:
            note("dispute_gate", True, human_approved=True)
        else:
            note("dispute_gate", False, reason="Dispute detected — requires human intervention", requires_human=True)
    else:
        note("dispute_gate", True)

    # Rule 3: High-value cases need human approval for financial actions
    if case.amount_paise > POLICY["require_human_approval_above_paise"]:
        if human_approved:
            note("high_value_gate", True, human_approved=True)
        elif action == RecoveryActionType.SEND_REMINDER:
            skip("high_value_gate", "Non-financial reminder permitted without approval")
        else:
            note("high_value_gate", False, amount=case.amount_paise, requires_human=True,
                 reason=f"Case value ({case.amount_paise} paise) exceeds ₹50,000 threshold.")
    else:
        note("high_value_gate", True)

    # Rule 4: Cap cumulative discount
    if action == RecoveryActionType.OFFER_DISCOUNT:
        pct = params.get("discount_percent", 0)
        proposed_paise = int(case.amount_paise * pct / 100)
        max_paise = int(case.amount_paise * POLICY["max_discount_percent"] / 100)
        if case.cumulative_discount_paise + proposed_paise > max_paise:
            note("discount_cap", False,
                 reason=f"Cumulative discount would exceed {POLICY['max_discount_percent']}%.")
        else:
            note("discount_cap", True)
    else:
        skip("discount_cap", "Action is not a discount offer")

    # Rule 5: Never retry a hard decline
    financial_actions = (
        RecoveryActionType.RETRY_CHARGE,
        RecoveryActionType.CREATE_PAYMENT_LINK,
        RecoveryActionType.SWITCH_RAIL,
    )
    if action in financial_actions:
        if diagnosis.root_cause_category == RootCauseCategory.HARD_DECLINE:
            note("block_hard_decline", False, reason="Policy forbids retrying hard declines.")
        else:
            note("block_hard_decline", True)
    else:
        skip("block_hard_decline", "Action is not a financial retry")

    # Rule 6: Max retry attempts
    if action in financial_actions:
        if case.retry_count > POLICY["max_retries"]:
            note("max_retries", False, reason=f"Max retries ({POLICY['max_retries']}) reached.")
        else:
            note("max_retries", True)
    else:
        skip("max_retries", "Action is not a financial retry")

    # Rule 7: Minimum AI confidence
    if decision.confidence_score < POLICY["min_confidence_score"] or \
       diagnosis.confidence_score < POLICY["min_confidence_score"]:
        if human_approved:
            note("min_confidence", True, human_approved=True)
        else:
            note("min_confidence", False, requires_human=True,
                 reason=f"AI confidence too low (decision={decision.confidence_score}, diagnosis={diagnosis.confidence_score}).")
    else:
        note("min_confidence", True)

    # Rule 8: RBI pre-debit notice for eNACH/NACH mandates
    if action in financial_actions:
        rail = params.get("target_rail", case.payment_rail or "").lower()
        if rail in ("enach", "nach", "emandate", "mandate"):
            delay = params.get("delay_hours", 0)
            if delay < POLICY["pre_debit_notice_hours"]:
                note("rbi_pre_debit", False,
                     reason=f"RBI requires {POLICY['pre_debit_notice_hours']}h pre-debit notice for {rail.upper()}. Proposed delay: {delay}h.")
            else:
                note("rbi_pre_debit", True)
        else:
            skip("rbi_pre_debit", "Not an eNACH/NACH mandate rail")
    else:
        skip("rbi_pre_debit", "Action is not a financial charge")

    if first_blocker:
        reason = first_blocker.get("reason", f"Blocked by rule: {first_blocker['rule']}")
        requires_human = first_blocker.get("requires_human", False)
        return PolicyResult(
            allowed=False,
            reason=f"Blocked: {reason}",
            requires_human_approval=requires_human,
            decision=decision,
            rules_checked=rules,
        )

    return PolicyResult(
        allowed=True,
        reason="Action allowed by policy.",
        decision=decision,
        rules_checked=rules,
    )
