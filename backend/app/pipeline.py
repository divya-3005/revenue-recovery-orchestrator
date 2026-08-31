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

  run_follow_up_check(db)
    The real-time re-loop: finds cases sitting PAYMENT_PENDING with no
    payment for POLICY['follow_up_after_hours'], and re-runs steps 3-9 on
    them with an escalated contact number (drives comms' gentle->firm->final
    tone ramp and ai's channel escalation), bounded by
    POLICY['max_follow_ups']. Without this, a case that received one
    payment link and was never paid just sat PAYMENT_PENDING forever —
    nothing ever checked back in, so "the system has memory, not a one-shot
    script" wasn't actually true at runtime.

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

        # contact_number drives the tone/channel escalation in comms + ai.
        # It folds in any prior follow-up rounds so a case that's already
        # been re-engaged a couple of times keeps escalating tone/channel
        # instead of resetting to "gentle" / "email" on this fresh call.
        contact_number = attempt + 1 + case.follow_up_count

        outcome = _run_attempt(db, case, provider, executor, contact_number)

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


def run_follow_up_check(db: Session, now: Optional[datetime] = None) -> list:
    """
    Feature 7's re-loop, made real. Scans for PAYMENT_PENDING cases that
    haven't been touched in POLICY['follow_up_after_hours'] and re-engages
    each one: re-diagnose, re-decide (the AI now sees more contacts on the
    case), re-check policy, and re-contact with an escalated tone/channel.
    Bounded by POLICY['max_follow_ups'] (a Feature 8 stopping rule) — after
    that many follow-ups with no payment, the case is escalated instead.

    Call this from a scheduler (see ENABLE_FOLLOW_UP_SCHEDULER in main.py)
    or manually via POST /api/v1/jobs/run-follow-ups. It's a plain function,
    not a route, specifically so it's directly unit-testable and so a real
    cron / Celery-beat job can call it without going through HTTP.
    """
    provider = AIProvider()
    executor = Executor()
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=POLICY["follow_up_after_hours"])

    stale_cases = (
        db.query(RecoveryCase)
        .filter(RecoveryCase.status == CaseStatus.PAYMENT_PENDING)
        .filter(RecoveryCase.updated_at <= cutoff)
        .all()
    )

    results = []
    for case in stale_cases:
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

        case.follow_up_count += 1
        db.commit()
        contact_number = case.retry_count + case.follow_up_count + 1

        outcome = _run_attempt(db, case, provider, executor, contact_number, is_follow_up=True)
        if outcome["status"] == "execution_failed":
            # Execution itself failed on this follow-up pass (e.g. a
            # transient Razorpay error) — leave the case PAYMENT_PENDING;
            # it's picked up again next follow-up window, still bounded by
            # max_follow_ups above, so this can't loop forever.
            results.append({"case_id": case.id, "status": "payment_pending", "reason": outcome["reason"]})
        else:
            results.append({"case_id": case.id, **outcome["result"]})

    return results


def _run_attempt(
    db: Session,
    case: RecoveryCase,
    provider: AIProvider,
    executor: Executor,
    contact_number: int,
    is_follow_up: bool = False,
) -> dict:
    """
    One pass through diagnose -> decide -> policy -> comms -> execute for a
    single case. Shared by run_pipeline (initial pass + execution-failure
    retries) and run_follow_up_check (real-time re-engagement passes).

    Returns either:
      {"status": "execution_failed", "reason": <str>}
        — the caller should retry (run_pipeline) or leave it PAYMENT_PENDING
          for the next follow-up window (run_follow_up_check).
      {"status": <terminal-ish>, "result": {...}}
        — `result` is exactly the dict run_pipeline / run_follow_up_check
          hand back to their own caller.
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
            case.pending_decision_json = json.loads(decision.canonical_json())
            case.pending_diagnosis_json = diagnosis.model_dump(mode="json")
            case.pending_decision_id = decision.decision_id
            case.pending_decision_hash = decision.canonical_hash()
            _audit(db, case.id, "APPROVAL_REQUESTED",
                "Human approval required for this action.", policy.reason)
            db.commit()
            result = {"status": "awaiting_approval", "reason": policy.reason}
            return {"status": "awaiting_approval", "result": result}
        else:
            # Policy blocked — case fails
            case.status = CaseStatus.FAILED
            _audit(db, case.id, "POLICY_BLOCKED", f"Recovery stopped: {policy.reason}", "")
            db.commit()
            result = {"status": "failed", "reason": policy.reason}
            return {"status": "failed", "result": result}

    # ── Generate communication (Feature 6) ──────────────────────────────
    # Only for actions that actually reach the customer — see
    # CUSTOMER_FACING_ACTIONS. escalate_to_human/stop are internal and
    # retry_charge is a silent saved-method attempt; none of them should
    # generate or bill for a customer message that's never sent.
    action = decision.recommended_action
    if action in CUSTOMER_FACING_ACTIONS:
        channel = decision.action_parameters.get("channel", "email")
        message = generate_message(case, diagnosis, contact_number, channel)
        case.latest_comms_preview = message
        case.latest_channel = channel
        case.cumulative_comms_cost_paise += 25  # ₹0.25 per message
        _audit(db, case.id, "COMMUNICATION_GENERATED",
            f"Message generated (contact #{contact_number}, channel: {channel})", message)
        db.commit()

    # ── Execute action (Feature 5) ──────────────────────────────────────
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

    # If the executor downgraded the channel (e.g. whatsapp -> sms, since
    # there's no real WhatsApp delivery integration), keep the case's
    # recorded channel truthful to what actually went out.
    if action in CUSTOMER_FACING_ACTIONS:
        actual_channel = exec_result.action_parameters_used.get("channel")
        if actual_channel:
            case.latest_channel = actual_channel

    db.commit()

    # ── Determine outcome (Feature 7) ────────────────────────────────────
    if exec_result.status in (ExecutionStatus.SUCCESS, ExecutionStatus.DRY_RUN):
        if exec_result.action_taken == RecoveryActionType.ESCALATE_TO_HUMAN:
            case.status = CaseStatus.ESCALATED
            db.commit()
            result = {"status": "escalated", "reason": "Case escalated to human review."}
            return {"status": "escalated", "result": result}

        if exec_result.action_taken == RecoveryActionType.STOP:
            result = _close_case(db, case, "AI_STOP", "AI decided to stop recovery.")
            return {"status": "closed", "result": result}

        # retry_charge, create_payment_link, offer_discount, send_reminder,
        # switch_rail all land here: the action succeeded, but final
        # confirmation (a webhook / customer follow-through) is still async.
        case.status = CaseStatus.PAYMENT_PENDING
        db.commit()
        result = {"status": "payment_pending", "reason": exec_result.reason}
        return {"status": "payment_pending", "result": result}
    else:
        return {"status": "execution_failed", "reason": exec_result.reason}


def _check_stopping_rules(db: Session, case: RecoveryCase) -> Optional[dict]:
    """
    Feature 8. Checks the stopping rules that apply regardless of which
    action would otherwise be taken. Returns a result dict if a rule fired
    (the caller should return it immediately), or None to keep going.
    Shared by run_pipeline and run_follow_up_check so a case can't dodge
    max-days/opt-out/PTP handling just because it's being re-checked on a
    follow-up pass instead of its first pass.
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
