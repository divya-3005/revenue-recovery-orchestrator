"""
Recovery Pipeline — the heart of the orchestrator.

This is the single function that runs the entire recovery flow for one case:
  1. Load case
  2. Check stopping rules (max days, opt-out, promise-to-pay)
  3. Diagnose (AI)
  4. Decide (AI)
  5. Policy check (deterministic guardrails)
  6. Generate customer communication
  7. Execute action (Razorpay or dry-run)
  8. Track result & update case status
  9. Log everything to audit trail

Features covered: 2, 3, 4, 5, 6, 7, 8, 9, 10
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import (
    RecoveryCase, AuditLog, CaseStatus, ApprovalStatus,
    DiagnosisResult, DecisionResult, ExecutionStatus, RecoveryActionType,
)
from app.ai import AIProvider, diagnose, decide
from app.policy import POLICY, evaluate_policy
from app.comms import generate_message
from app.executor import Executor

logger = logging.getLogger(__name__)


def run_pipeline(db: Session, case_id: str) -> dict:
    """
    Run the full recovery pipeline for a single case.
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

        # ── Stopping Rules (Feature 8) ───────────────────────────────

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
                return {"status": "waiting", "reason": f"Promise-to-pay: {case.promise_to_pay_date}"}
            else:
                # Promise broken — escalate
                case.status = CaseStatus.ESCALATED
                _audit(db, case.id, "PTP_BROKEN",
                    f"Promise-to-pay broken: committed to {case.promise_to_pay_date}, not received.",
                    "Auto-escalating broken promise.")
                db.commit()
                return {"status": "escalated", "reason": "Promise-to-pay broken"}

        # ── Step 1: Diagnose (Feature 2) ─────────────────────────────

        diagnosis = diagnose(case, provider)
        case.latest_diagnosis_category = diagnosis.root_cause_category.value
        case.latest_diagnosis_confidence = diagnosis.confidence_score
        case.latest_diagnosis_reasoning = diagnosis.reasoning
        _audit(db, case.id, "DIAGNOSIS_COMPLETED",
            f"Diagnosis: {diagnosis.root_cause_category.value} ({int(diagnosis.confidence_score * 100)}% confidence)",
            json.dumps(diagnosis.model_dump(mode="json")))
        db.commit()

        # ── Step 2: Decide (Feature 4) ───────────────────────────────

        decision = decide(case, diagnosis, provider)
        case.latest_action_recommended = decision.recommended_action.value
        _audit(db, case.id, "DECISION_PROPOSED",
            f"Proposed action: {decision.recommended_action.value}",
            json.dumps(decision.model_dump(mode="json")))
        db.commit()

        # ── Step 3: Policy check (Feature 3) ─────────────────────────

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
                    "Human approval required for this action.",
                    policy.reason)
                db.commit()
                return {"status": "awaiting_approval", "reason": policy.reason}
            else:
                # Policy blocked — case fails
                case.status = CaseStatus.FAILED
                _audit(db, case.id, "POLICY_BLOCKED",
                    f"Recovery stopped: {policy.reason}", "")
                db.commit()
                return {"status": "failed", "reason": policy.reason}

        # ── Step 4: Generate communication (Feature 6) ───────────────

        channel = decision.action_parameters.get("channel", "email")
        message = generate_message(case, diagnosis, attempt + 1, channel)
        case.latest_comms_preview = message
        case.latest_channel = channel
        case.cumulative_comms_cost_paise += 25  # ₹0.25 per message
        _audit(db, case.id, "COMMUNICATION_GENERATED",
            f"Message generated (attempt {attempt + 1}, channel: {channel})",
            message)
        db.commit()

        # ── Step 5: Execute action (Feature 5) ───────────────────────

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

        if channel:
            case.latest_channel = channel

        db.commit()

        # ── Step 6: Determine outcome (Feature 7) ────────────────────

        if exec_result.status in (ExecutionStatus.SUCCESS, ExecutionStatus.DRY_RUN):
            if exec_result.action_taken == RecoveryActionType.ESCALATE_TO_HUMAN:
                case.status = CaseStatus.ESCALATED
                db.commit()
                return {"status": "escalated", "reason": "Case escalated to human review."}

            if exec_result.action_taken == RecoveryActionType.STOP:
                return _close_case(db, case, "AI_STOP", "AI decided to stop recovery.")

            # Action succeeded — mark as payment pending
            case.status = CaseStatus.PAYMENT_PENDING
            db.commit()
            return {"status": "payment_pending", "reason": exec_result.reason}
        else:
            # Execution failed — retry if we have attempts left
            if attempt < max_attempts - 1:
                case.retry_count += 1
                case.status = CaseStatus.IN_PROGRESS
                _audit(db, case.id, "RETRY",
                    f"Retrying (attempt {attempt + 2}/{max_attempts})",
                    f"Previous execution failed: {exec_result.reason}")
                db.commit()
                continue
            # else: fall through to exhaustion handler

    # All attempts exhausted — escalate (Feature 9)
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    if case and case.status not in (CaseStatus.RECOVERED, CaseStatus.CLOSED):
        case.status = CaseStatus.ESCALATED
        _audit(db, case.id, "ATTEMPTS_EXHAUSTED",
            f"All {max_attempts} attempts exhausted. Escalated for human review.", "")
        db.commit()

    return {"status": "escalated", "reason": f"All {max_attempts} recovery attempts exhausted."}


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
