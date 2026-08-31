"""
Recovery Pipeline — the heart of the orchestrator.

Two entry points share one core attempt (_run_attempt):

  run_pipeline(db, case_id)
    The initial pass for a case, plus same-call retries when the *execution*
    itself fails (e.g. a transient Razorpay API error):
      1. Load case
      2. Check stopping rules (max days, opt-out, promise-to-pay)
      3. Diagnose (AI)
      4. Decide (AI)
      5. Policy check (deterministic guardrails)
      6. Generate customer communication (only for customer-facing actions)
      7. Execute action (Razorpay or dry-run)
      8. Track result & update case status
      9. Log everything to audit trail

  run_follow_up_check(db, force=False)
    The real-time re-loop: finds cases sitting PAYMENT_PENDING with no
    payment for POLICY['follow_up_after_hours'], and re-engages each one
    with an escalated tone/channel, bounded by POLICY['max_follow_ups'].
    force=True bypasses the time window (for demos).

Features covered: 2, 3, 4, 5, 6, 7, 8, 9, 10
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models import (
    RecoveryCase, AuditLog, CaseStatus, ApprovalStatus,
    ExecutionStatus, RecoveryActionType,
)
from app.ai import AIProvider, diagnose, decide
from app.policy import POLICY, evaluate_policy
from app.comms import generate_message
from app.executor import Executor

logger = logging.getLogger(__name__)

# Actions that actually put a message in front of the customer. Used to
# gate Step 6 (comms generation + its ₹0.25 cost) — escalate_to_human and
# stop are internal routing decisions with no customer contact, and
# retry_charge is a deliberately *silent* saved-method retry, so none of
# them should generate or bill for a customer-facing message.
CUSTOMER_FACING_ACTIONS = (
    RecoveryActionType.CREATE_PAYMENT_LINK,
    RecoveryActionType.OFFER_DISCOUNT,
    RecoveryActionType.SEND_REMINDER,
    RecoveryActionType.SWITCH_RAIL,
)


def run_pipeline(db: Session, case_id: str) -> dict:
    """
    Run the recovery pipeline for a single case: an initial attempt, plus
    up to POLICY['max_retries'] more if the *execution step itself* fails.
    Returns a dict with the final status and what happened.
    """
    provider = AIProvider()
    executor = Executor()
    max_attempts = POLICY["max_retries"] + 1  # initial try + retries

    for attempt in range(max_attempts):
        # Reload case each iteration (picks up updated retry_count, status)
        case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
        if not case:
            return {"status": "error", "reason": f"Case {case_id} not found"}

        # Skip if case is already in a terminal state
        if case.status in (CaseStatus.RECOVERED, CaseStatus.CLOSED, CaseStatus.FAILED):
            return {"status": case.status.value, "reason": "Case already resolved"}

        # Mark in-progress on first attempt
        if attempt == 0 and case.status == CaseStatus.OPEN:
            case.status = CaseStatus.IN_PROGRESS
            db.commit()

        stop_result = _check_stopping_rules(db, case)
        if stop_result is not None:
            return stop_result

        outcome = _run_attempt(db, case, provider, executor)

        if outcome["status"] == "execution_failed":
            if attempt < max_attempts - 1:
                case.retry_count += 1
                case.status = CaseStatus.IN_PROGRESS
                _audit(db, case.id, "RETRY",
                    f"Retrying (attempt {attempt + 2}/{max_attempts})",
                    f"Previous execution failed: {outcome['reason']}")
                db.commit()
                continue
            # else: fall through to exhaustion handler
        else:
            return outcome["result"]

    # All attempts exhausted — escalate (Feature 9)
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    if case and case.status not in (CaseStatus.RECOVERED, CaseStatus.CLOSED):
        case.status = CaseStatus.ESCALATED
        _audit(db, case.id, "ATTEMPTS_EXHAUSTED",
            f"All {max_attempts} attempts exhausted. Escalated for human review.", "")
        db.commit()

    return {"status": "escalated", "reason": f"All {max_attempts} recovery attempts exhausted."}


def run_follow_up_check(db: Session, now: Optional[datetime] = None, force: bool = False) -> list:
    """
    Feature 7's re-loop. Scans for PAYMENT_PENDING cases that haven't been
    touched in POLICY['follow_up_after_hours'] and re-engages each one with
    an escalated tone/channel, bounded by POLICY['max_follow_ups'].

    force=True bypasses the follow_up_after_hours window and re-engages
    every unpaid PAYMENT_PENDING case immediately. Intended for demos and
    manual triggers; max_follow_ups still bounds it.
    """
    provider = AIProvider()
    executor = Executor()
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=POLICY["follow_up_after_hours"])

    query = db.query(RecoveryCase).filter(RecoveryCase.status == CaseStatus.PAYMENT_PENDING)
    if not force:
        query = query.filter(RecoveryCase.updated_at <= cutoff)
    stale_cases = query.all()

    results = []
    for case in stale_cases:
        # _check_stopping_rules handles all policy-level blocks (max retries, hard declines).
        # We share this with the main pipeline so follow-ups respect the same guardrails.
        stop_result = _check_stopping_rules(db, case)
        if stop_result is not None:
            results.append({"case_id": case.id, **stop_result})
            continue

        if case.follow_up_count >= POLICY["max_follow_ups"]:
            case.status = CaseStatus.ESCALATED
            _audit(db, case.id, "FOLLOWUPS_EXHAUSTED",
                f"No payment after {case.follow_up_count} follow-up(s) "
                f"({POLICY['max_follow_ups']} allowed). Escalated for human review.", "")
            db.commit()
            results.append({"case_id": case.id, "status": "escalated", "reason": "Follow-ups exhausted"})
            continue

        if case.scheduled_for:
            sched = case.scheduled_for.replace(tzinfo=timezone.utc) if case.scheduled_for.tzinfo is None else case.scheduled_for
            if sched > now:
                results.append({"case_id": case.id, "status": "scheduled",
                    "reason": f"Still within compliance/delay window until {sched.isoformat()}."})
                continue

        case.follow_up_count += 1
        if force:
            _audit(db, case.id, "FOLLOW_UP_FORCED",
                "Follow-up window bypassed by a manual/demo trigger.",
                f"Normal window is {POLICY['follow_up_after_hours']}h.")
        db.commit()

        outcome = _run_attempt(db, case, provider, executor, is_follow_up=True)
        if outcome["status"] == "execution_failed":
            results.append({"case_id": case.id, "status": "payment_pending", "reason": outcome["reason"]})
        else:
            results.append({"case_id": case.id, **outcome["result"]})

    return results


def _run_attempt(
    db: Session,
    case: RecoveryCase,
    provider: AIProvider,
    executor: Executor,
    is_follow_up: bool = False,
) -> dict:
    """
    One pass through diagnose -> decide -> policy -> comms -> execute for a
    single case. Shared by run_pipeline and run_follow_up_check.
    """
    if is_follow_up:
        _audit(db, case.id, "FOLLOW_UP_STARTED",
            f"Re-engaging unpaid case — follow-up #{case.follow_up_count} "
            f"after {POLICY['follow_up_after_hours']}h with no payment.", "")
        db.commit()

    # ── Diagnose (Feature 2) ────────────────────────────────────────────
    diagnosis = diagnose(case, provider)
    case.latest_diagnosis_category = diagnosis.root_cause_category.value
    case.latest_diagnosis_confidence = diagnosis.confidence_score
    case.latest_diagnosis_reasoning = diagnosis.reasoning
    _audit(db, case.id, "DIAGNOSIS_COMPLETED",
        f"Diagnosis: {diagnosis.root_cause_category.value} ({int(diagnosis.confidence_score * 100)}% confidence)",
        json.dumps(diagnosis.model_dump(mode="json")))
    db.commit()

    # ── Decide (Feature 4) ──────────────────────────────────────────────
    decision = decide(case, diagnosis, provider)
    case.latest_action_recommended = decision.recommended_action.value
    _audit(db, case.id, "DECISION_PROPOSED",
        f"Proposed action: {decision.recommended_action.value}",
        json.dumps(decision.model_dump(mode="json")))
    db.commit()

    # ── Policy check (Feature 3) ────────────────────────────────────────
    policy = evaluate_policy(case, decision, diagnosis)
    _audit(db, case.id, "POLICY_EVALUATED",
        f"Policy {'APPROVED' if policy.allowed else 'REJECTED'}: {policy.reason}",
        json.dumps({"allowed": policy.allowed, "rules": policy.rules_checked}))
    db.commit()

    if not policy.allowed:
        if policy.requires_human_approval:
            # Route to human approval queue (Feature 9 + 15)
            case.status = CaseStatus.AWAITING_APPROVAL
            case.approval_status = ApprovalStatus.PENDING
            # store full model_dump so _execute_approved can validate it 
            # (canonical_json() omits confidence_score).
            case.pending_decision_json = decision.model_dump(mode="json")
            case.pending_diagnosis_json = diagnosis.model_dump(mode="json")
            case.pending_decision_id = decision.decision_id
            case.pending_decision_hash = decision.canonical_hash()
            _audit(db, case.id, "APPROVAL_REQUESTED",
                "Human approval required for this action.", policy.reason)
            db.commit()
            result = {"status": "awaiting_approval", "reason": policy.reason}
            return {"status": "awaiting_approval", "result": result}
        else:
            # A hard policy block means "not this action", not "give up on the money".
            # Escalate so a human can work it, rather than writing the case off.
            case.status = CaseStatus.ESCALATED
            _audit(db, case.id, "POLICY_BLOCKED",
                f"Action blocked, case handed to human review: {policy.reason}",
                "A guardrail rejected the proposed action; the case is still recoverable.")
            db.commit()
            result = {"status": "escalated", "reason": policy.reason}
            return {"status": "escalated", "result": result}

    # ── Generate communication (Feature 6) ──────────────────────────────
    # Only for actions that actually reach the customer.
    # use case.contact_count (customer-facing contacts only),
    # not attempt-based count that includes silent retry_charge.
    action = decision.recommended_action
    if action in CUSTOMER_FACING_ACTIONS:
        case.contact_count += 1
        channel = decision.action_parameters.get("channel", "email")
        message = generate_message(case, diagnosis, case.contact_count, channel)
        case.latest_comms_preview = message
        case.latest_channel = channel
        case.cumulative_comms_cost_paise += 25  # ₹0.25 per message
        _audit(db, case.id, "COMMUNICATION_GENERATED",
            f"Message generated (contact #{case.contact_count}, channel: {channel})", message)
        db.commit()
    # ── Execute action (Feature 5) ──────────────────────────────────────
    delay_hours = decision.action_parameters.get("delay_hours", 0)
    if delay_hours > 0 and action in CUSTOMER_FACING_ACTIONS:
        case.scheduled_for = datetime.now(timezone.utc) + timedelta(hours=delay_hours)
    else:
        case.scheduled_for = None
        
    exec_result = executor.execute(case, decision)
    _audit(db, case.id, "ACTION_EXECUTED",
        f"Execution {exec_result.status.value}: {exec_result.action_taken.value}",
        json.dumps({
            "reason": exec_result.reason,
            "external_ref": exec_result.external_reference_id,
            "params": exec_result.action_parameters_used,
        }))

    # Track discount if applied
    if exec_result.action_taken == RecoveryActionType.OFFER_DISCOUNT:
        discount = exec_result.action_parameters_used.get("discount_applied_paise", 0)
        if discount:
            case.cumulative_discount_paise += discount

    # If the executor downgraded the channel (e.g. whatsapp -> sms),
    # keep the recorded channel truthful.
    if action in CUSTOMER_FACING_ACTIONS:
        actual_channel = exec_result.action_parameters_used.get("channel")
        if actual_channel:
            case.latest_channel = actual_channel

    db.commit()

    # ── Determine outcome (Feature 7) ────────────────────────────────────
    if exec_result.status in (ExecutionStatus.SUCCESS, ExecutionStatus.DRY_RUN):
        if exec_result.action_taken == RecoveryActionType.ESCALATE_TO_HUMAN:
            case.status = CaseStatus.ESCALATED
            _audit(db, case.id, "ESCALATED",
                "Case handed to the human review queue.", diagnosis.reasoning)
            db.commit()
            result = {"status": "escalated", "reason": "Case escalated to human review."}
            return {"status": "escalated", "result": result}

        if exec_result.action_taken == RecoveryActionType.STOP:
            result = _close_case(db, case, "AI_STOP", "AI decided to stop recovery.")
            return {"status": "closed", "result": result}

        # All other successful actions land here: awaiting async payment.
        case.status = CaseStatus.PAYMENT_PENDING
        if case.scheduled_for:
            _audit(db, case.id, "SCHEDULED",
                f"Action complete — next step scheduled for {case.scheduled_for.strftime('%Y-%m-%d %H:%M:%S UTC')}.", exec_result.reason)
        else:
            _audit(db, case.id, "AWAITING_PAYMENT",
                f"Action complete — awaiting customer payment. "
                f"Next follow-up in {POLICY['follow_up_after_hours']}h.", exec_result.reason)
        db.commit()
        result = {"status": "payment_pending", "reason": exec_result.reason}
        return {"status": "payment_pending", "result": result}
    else:
        return {"status": "execution_failed", "reason": exec_result.reason}


def _check_stopping_rules(db: Session, case: RecoveryCase) -> Optional[dict]:
    """
    Feature 8. Checks the stopping rules that apply regardless of which
    action would otherwise be taken.
    """
    # Stop A: Max days pursued
    if case.created_at:
        created = case.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - created).days
        if days >= POLICY["max_days_pursued"]:
            return _close_case(db, case, "MAX_DAYS",
                f"Max pursuit period ({POLICY['max_days_pursued']} days) reached.")

    # Stop B: Customer opt-out
    if case.opted_out:
        return _close_case(db, case, "OPT_OUT",
            "Customer opted out of recovery communications.")

    # Stop C: Promise-to-pay still in future — suppress recovery
    if case.promise_to_pay_date:
        today = datetime.now(timezone.utc).date()
        if case.promise_to_pay_date > today:
            _audit(db, case.id, "PTP_WAIT",
                f"Suppressing recovery — customer promised to pay by {case.promise_to_pay_date}.",
                "Recovery actions suspended until promise date.")
            db.commit()
            return {"status": "waiting", "reason": f"Promise-to-pay: {case.promise_to_pay_date}"}
        else:
            # Promise broken — escalate
            case.status = CaseStatus.ESCALATED
            _audit(db, case.id, "PTP_BROKEN",
                f"Promise-to-pay broken: committed to {case.promise_to_pay_date}, not received.",
                "Auto-escalating broken promise.")
            db.commit()
            return {"status": "escalated", "reason": "Promise-to-pay broken"}

    return None


def _close_case(db: Session, case: RecoveryCase, rule: str, reason: str) -> dict:
    """Close a case due to a stopping rule."""
    case.status = CaseStatus.CLOSED
    _audit(db, case.id, "STOPPING_RULE",
        f"Stopping rule [{rule}]: {reason}", "")
    db.commit()
    return {"status": "closed", "reason": reason}


def _audit(db: Session, case_id: str, action_type: str, description: str, reasoning: str):
    """Append one entry to the audit trail (Feature 10)."""
    db.add(AuditLog(
        case_id=case_id,
        action_type=action_type,
        description=description,
        reasoning=reasoning,
    ))
