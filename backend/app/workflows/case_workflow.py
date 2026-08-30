"""
Inngest Durable Workflow — the heart of the recovery pipeline.

Flow per case:
  1. Load case from DB
  2. Mark IN_PROGRESS
  3. Loop (up to MAX_RETRIES + 1 attempts):
     a. Diagnose (AI)
     b. Decide (AI)
     c. Policy check (deterministic)
     d. Generate communication message
     e. Execute action (Razorpay or internal)
     f. If success → mark PAYMENT_PENDING/ESCALATED/CLOSED
     g. If failed → increment retry, loop back
  4. If all attempts exhausted → mark FAILED

Each step.run() is memoized by Inngest — if the process crashes and restarts,
completed steps are skipped automatically.
"""

from inngest import Context, TriggerEvent, Step, Concurrency
from sqlalchemy import select
import os

from app.inngest_client import inngest_client
from app import database
from app import models
from app.models import CaseStatus
from app.domain import (
    RecoveryCaseContext, DiagnosisResult, DecisionResult,
    PolicyApprovedDecision, PolicyEvaluationResult, ExecutionResult, ExecutionStatus,
    RecoveryActionType,
)
from app.workflow import (
    run_diagnosis_step, run_decision_step,
    run_policy_step, run_communication_step, run_execution_step,
)
from app.policy import PolicyConfig
from app.ai.provider import FallbackProvider, GeminiProvider, GroqProvider, AnthropicProvider
from app.db.audit_repository import update_case_status


def _get_inngest_function():
    @inngest_client.create_function(
        fn_id="process-recovery-case",
        trigger=TriggerEvent(event="case.received"),
        concurrency=[Concurrency(limit=1, key="event.data.case_id")]
    )
    async def process_case_workflow(ctx: Context, step):
        return await inner_process_case_workflow(ctx, step)
    return process_case_workflow


async def inner_process_case_workflow(ctx, step):
    """Main workflow logic — exposed for testing."""
    case_id = ctx.event.data["case_id"]
    max_attempts = PolicyConfig.MAX_RETRIES + 1  # initial try + retries

    # ── Helper closures ──────────────────────────────────────────────
    
    async def _trigger_stopping_rule(rule_name: str, reason: str):
        def _close_and_audit():
            db_sess = database.SessionLocal()
            try:
                from app.models import RecoveryCase, AuditLog
                from app.db.audit_repository import _generate_audit_id
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                db_sess.query(RecoveryCase).filter(RecoveryCase.id == case_id).update({
                    "status": CaseStatus.CLOSED.value
                })
                audit_id = _generate_audit_id(case_id, f"STOP_{rule_name}", -1)
                stmt = pg_insert(AuditLog).values(
                    id=audit_id, case_id=case_id, action_type="STOPPING_RULE_TRIGGERED",
                    description=f"Stopping Rule: {rule_name}",
                    reasoning=reason
                ).on_conflict_do_nothing()
                db_sess.execute(stmt)
                db_sess.commit()
            finally:
                db_sess.close()
        await step.run(f"stop_rule_{rule_name}", _close_and_audit)
        return {"status": "closed", "reason": reason}

    def _load_case():
        db = database.SessionLocal()
        try:
            db_case = db.execute(
                select(models.RecoveryCase).where(models.RecoveryCase.id == case_id)
            ).scalars().first()
            if not db_case:
                raise ValueError(f"Case {case_id} not found")
            return RecoveryCaseContext.model_validate(db_case).model_dump()
        finally:
            db.close()

    def _set_status(new_status, inc_retry=False):
        db = database.SessionLocal()
        try:
            return update_case_status(db, case_id, new_status, increment_retry=inc_retry)
        finally:
            db.close()

    def _get_provider_instance(name: str):
        if name == "gemini":
            return GeminiProvider()
        elif name == "groq":
            return GroqProvider()
        elif name == "anthropic":
            return AnthropicProvider()
        else:
            raise ValueError(f"Unknown AI provider: {name}")

    def _get_provider():
        primary_name = os.getenv("AI_PRIMARY_PROVIDER", "gemini").lower()
        fallback_name = os.getenv("AI_FALLBACK_PROVIDER", "groq").lower()
        
        primary = _get_provider_instance(primary_name)
        fallback = _get_provider_instance(fallback_name)
        
        return FallbackProvider(primary, fallback)

    # ── Mark case as in progress ─────────────────────────────────────

    updated = await step.run("mark_in_progress", lambda: _set_status(CaseStatus.IN_PROGRESS))
    if not updated:
        return {"status": "skipped", "reason": "Case already in terminal state"}

    # ── Recovery loop ────────────────────────────────────────────────

    for attempt in range(max_attempts):

        # 1. Load / reload case (picks up updated retry_count)
        case_dict = await step.run(f"load_case_{attempt}", _load_case)
        case_domain = RecoveryCaseContext.model_validate(case_dict)

        # Stopping Rule A: Max 14-day pursuit window
        if case_domain.created_at:
            from datetime import datetime, timezone
            days_pursued = (datetime.now(timezone.utc) - case_domain.created_at.replace(tzinfo=timezone.utc if case_domain.created_at.tzinfo is None else case_domain.created_at.tzinfo)).days
            if days_pursued >= PolicyConfig.MAX_DAYS_PURSUED:
                return await _trigger_stopping_rule("MAX_DAYS", f"Max pursuit period ({PolicyConfig.MAX_DAYS_PURSUED} days) reached after {days_pursued} days.")

        # Stopping Rule B: Customer opt-out
        payload = case_domain.raw_signal_payload or {}
        if case_domain.opted_out or payload.get("customer_opted_out") or payload.get("opt_out"):
            return await _trigger_stopping_rule("OPT_OUT", "Customer has opted out of recovery communications.")

        # Stopping Rule C: Promise-to-Pay — if customer has committed to a date, respect it
        if case_domain.promise_to_pay_date:
            from datetime import datetime, timezone
            ptp_date = case_domain.promise_to_pay_date
            today = datetime.now(timezone.utc).date()

            if ptp_date > today:
                # Promise is still in the future — suppress AI loop, sleep until the committed date
                hours_to_wait = max(1, (ptp_date - today).days * 24)
                db_ptp = database.SessionLocal()
                try:
                    from app.db.audit_repository import _generate_audit_id
                    from app.models import AuditLog
                    from sqlalchemy.dialects.postgresql import insert as pg_insert
                    audit_id = _generate_audit_id(case_id, f"PTP_WAIT_{attempt}", case_domain.retry_count)
                    stmt = pg_insert(AuditLog).values(
                        id=audit_id, case_id=case_id, action_type="PTP_WAIT",
                        description=f"Suppressing reminders — customer promised to pay by {ptp_date}. Sleeping {hours_to_wait}h.",
                        reasoning=f"Promise-to-pay date: {ptp_date}. Standard recovery actions suspended."
                    ).on_conflict_do_nothing()
                    db_ptp.execute(stmt)
                    db_ptp.commit()
                finally:
                    db_ptp.close()

                await step.sleep(f"ptp_wait_{attempt}", f"{hours_to_wait}h")

                # Re-check case after waking — did payment come in?
                woken_dict = await step.run(f"ptp_reload_{attempt}", _load_case)
                woken = RecoveryCaseContext.model_validate(woken_dict)
                if woken.status in [CaseStatus.RECOVERED, CaseStatus.PARTIALLY_RECOVERED, CaseStatus.FAILED, CaseStatus.CLOSED]:
                    return {"status": woken.status.value, "reason": "Case resolved during promise-to-pay window."}

                # Promise broken — escalate
                updated = await step.run(
                    f"ptp_broken_{attempt}",
                    lambda: _set_status(CaseStatus.ESCALATED)
                )
                if not updated:
                    return {"status": "skipped", "reason": "Case already in terminal state"}
                return {
                    "status": "escalated",
                    "reason": f"Promise to pay broken: customer committed to pay by {ptp_date} but payment was not received."
                }

            else:
                # PTP date has already passed — escalate immediately
                updated = await step.run(
                    f"ptp_overdue_{attempt}",
                    lambda: _set_status(CaseStatus.ESCALATED)
                )
                if not updated:
                    return {"status": "skipped", "reason": "Case already in terminal state"}
                return {
                    "status": "escalated",
                    "reason": f"Promise to pay overdue: customer committed to pay by {ptp_date}, payment not received."
                }

        # 2. Diagnose
        def _diagnose(case=case_domain):
            return run_diagnosis_step(case, _get_provider()).model_dump()

        diagnosis_dict = await step.run(f"diagnose_{attempt}", _diagnose)
        diagnosis = DiagnosisResult.model_validate(diagnosis_dict)

        # 3. Decide
        def _decide(case=case_domain, diag=diagnosis):
            return run_decision_step(case, diag, _get_provider()).model_dump()

        decision_dict = await step.run(f"decide_{attempt}", _decide)
        decision = DecisionResult.model_validate(decision_dict)

        # 4. Policy check
        def _policy(case=case_domain, dec=decision, diag=diagnosis):
            return run_policy_step(case, dec, diag).model_dump()

        policy_dict = await step.run(f"policy_{attempt}", _policy)
        policy_eval = PolicyEvaluationResult.model_validate(policy_dict)

        # 5. Handle policy rejection
        if not policy_eval.allowed or not policy_eval.approved_decision:
            # High-value cases needing human sign-off → request approval
            if policy_eval.requires_human_approval:
                def _persist_approval_request(c_id=case_id, dec=decision, diag=diagnosis):
                    db_sess = database.SessionLocal()
                    try:
                        from app.models import RecoveryCase, ApprovalStatus, AuditLog
                        from app.db.audit_repository import _generate_audit_id
                        from sqlalchemy.dialects.postgresql import insert as pg_insert
                        import json
                        decision_json = dec.canonical_json()
                        decision_hash = dec.canonical_hash()
                        
                        db_sess.query(RecoveryCase).filter(RecoveryCase.id == c_id).update({
                            "pending_decision_json": json.loads(decision_json),
                            "pending_diagnosis_json": diag.model_dump(mode='json'),
                            "pending_decision_id": dec.decision_id,
                            "pending_decision_hash": decision_hash,
                            "approval_status": ApprovalStatus.PENDING.value,
                            "status": CaseStatus.AWAITING_APPROVAL.value
                        })
                        
                        audit_id = _generate_audit_id(c_id, f"APPROVAL_REQ_{attempt}", case_domain.retry_count)
                        stmt = pg_insert(AuditLog).values(
                            id=audit_id, case_id=c_id, action_type="APPROVAL_REQUESTED",
                            description="Human approval requested for high-value/sensitive action",
                            reasoning=policy_eval.reason
                        ).on_conflict_do_nothing()
                        db_sess.execute(stmt)
                        db_sess.commit()
                    finally:
                        db_sess.close()
                
                await step.run(f"request_approval_{attempt}", _persist_approval_request)
                
                # Halt workflow. The API endpoint will dispatch a new event when approved/rejected.
                return {"status": CaseStatus.AWAITING_APPROVAL.value, "reason": policy_eval.reason}
                
            # Everything else (hard decline, max retries, etc.) → fail
            updated = await step.run(f"reject_{attempt}",
                                     lambda: _set_status(CaseStatus.FAILED))
            if not updated:
                return {"status": "skipped", "reason": "Case already in terminal state before rejecting"}
            return {"status": "policy_rejected", "reason": policy_eval.reason}

        # 6. Generate customer communication
        def _comms(case=case_domain, diag=diagnosis, dec=decision, att=attempt):
            channel = dec.action_parameters.get("channel", "email")
            return run_communication_step(case, diag, att + 1, channel)

        await step.run(f"comms_{attempt}", _comms)

        # 7. Execute the approved action
        approved = policy_eval.approved_decision

        def _execute(case=case_domain, appr=approved):
            from app.execution.executor import RazorpayExecutor
            executor = RazorpayExecutor()
            return run_execution_step(case, appr, executor).model_dump()

        exec_dict = await step.run(f"execute_{attempt}", _execute)
        exec_result = ExecutionResult.model_validate(exec_dict)

        # 8. Determine outcome
        action = decision.recommended_action

        if exec_result.status in [ExecutionStatus.SUCCESS, ExecutionStatus.DRY_RUN]:
            if action == RecoveryActionType.ESCALATE_TO_HUMAN:
                updated = await step.run(f"finalize_{attempt}", lambda: _set_status(CaseStatus.ESCALATED))
                if not updated:
                    return {"status": "skipped", "reason": "Case already in terminal state before escalating"}
                return {"status": CaseStatus.ESCALATED.value, "execution": exec_dict}
            elif action == RecoveryActionType.STOP:
                return await _trigger_stopping_rule("AI_DECISION", "AI chose to stop recovery.")
            else:
                # Execution Success -> PAYMENT_PENDING. 
                # (We rely exclusively on process_payment_confirmation / webhooks to move to RECOVERED)
                updated = await step.run(f"payment_pending_{attempt}", lambda: _set_status(CaseStatus.PAYMENT_PENDING))
                if not updated:
                    return {"status": "skipped", "reason": "Case already in terminal state before payment pending"}
                
                delay_hours = decision.action_parameters.get("delay_hours", 24)
                delay_hours = max(1, min(delay_hours, 168))
                
                await step.sleep(f"wait_for_payment_{attempt}", f"{delay_hours}h")
                
                woken_case_dict = await step.run(f"load_woken_case_{attempt}", _load_case)
                woken_case = RecoveryCaseContext.model_validate(woken_case_dict)
                
                if woken_case.status in [CaseStatus.RECOVERED, CaseStatus.PARTIALLY_RECOVERED, CaseStatus.FAILED, CaseStatus.CLOSED]:
                    return {"status": woken_case.status.value, "execution": exec_dict}
                
                # Fall through to retry block if payment wasn't confirmed

        # 9. Execution failed — can we retry?
        if attempt < max_attempts - 1:
            updated = await step.run(f"retry_{attempt}",
                                     lambda: _set_status(CaseStatus.IN_PROGRESS, inc_retry=True))
            if not updated:
                return {"status": "skipped", "reason": "Case already in terminal state before retry"}
            continue  # loop back to diagnose with updated state

    # 10. All attempts exhausted — escalate to human review queue
    updated = await step.run("finalize_escalated", lambda: _set_status(CaseStatus.ESCALATED))
    if not updated:
        return {"status": "skipped", "reason": "Case already in terminal state"}
    return {"status": "escalated", "reason": f"All {max_attempts} recovery attempts exhausted without resolution. Escalated for human review."}


@inngest_client.create_function(
    fn_id="execute-approved-action",
    name="Execute Approved Action",
    trigger=TriggerEvent(event="case.execute_approved"),
)
async def execute_approved_action(ctx: Context, step: Step) -> dict:
    """
    Triggered when a human approves an action via the API.
    This fetches the pending decision and directly executes it.
    """
    case_id = ctx.event.data["case_id"]

    def _load_case():
        db = database.SessionLocal()
        try:
            from app.models import RecoveryCase
            case_orm = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
            if not case_orm:
                raise ValueError(f"Case {case_id} not found")
            return RecoveryCaseContext.model_validate(case_orm).model_dump()
        finally:
            db.close()

    case_dict = await step.run("load_approved_case", _load_case)
    case_domain = RecoveryCaseContext.model_validate(case_dict)

    if not case_domain.pending_decision_json:
        return {"status": "error", "reason": "No pending decision found"}

    from app.models import ApprovalStatus
    if case_domain.approval_status != ApprovalStatus.APPROVED.value:
        return {"status": "error", "reason": f"Execution rejected: approval_status is '{case_domain.approval_status}', expected APPROVED"}

    decision = DecisionResult.model_validate(case_domain.pending_decision_json)

    if case_domain.approved_decision_id and decision.decision_id and case_domain.approved_decision_id != decision.decision_id:
        return {"status": "error", "reason": f"Execution rejected: decision_id '{decision.decision_id}' does not match approved_decision_id '{case_domain.approved_decision_id}'"}

    if case_domain.approved_decision_hash and case_domain.approved_decision_hash != decision.canonical_hash():
        return {"status": "error", "reason": "Execution rejected: decision_hash does not match approved_decision_hash"}
    
    approved_decision = PolicyApprovedDecision(
        decision=decision,
        policy_reason="Human manually approved",
        idempotency_key=f"manual_appr_{case_id}_{case_domain.retry_count}",
        requires_human_approval=True
    )

    def _execute():
        from app.execution.executor import RazorpayExecutor
        executor = RazorpayExecutor()
        return run_execution_step(case_domain, approved_decision, executor).model_dump()

    exec_dict = await step.run("execute_approved", _execute)
    exec_result = ExecutionResult.model_validate(exec_dict)
    
    def _set_status(new_status):
        db = database.SessionLocal()
        try:
            return update_case_status(db, case_id, new_status)
        finally:
            db.close()
            
    if exec_result.status in [ExecutionStatus.SUCCESS, ExecutionStatus.DRY_RUN]:
        await step.run("set_payment_pending", lambda: _set_status(CaseStatus.PAYMENT_PENDING))
        return {"status": "payment_pending"}
    else:
        await step.run("set_failed", lambda: _set_status(CaseStatus.FAILED))
        return {"status": "failed", "reason": exec_result.reason}

process_case_workflow = _get_inngest_function()
