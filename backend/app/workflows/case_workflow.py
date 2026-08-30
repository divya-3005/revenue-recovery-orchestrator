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
     f. If success → mark RECOVERED/ESCALATED/CLOSED, done
     g. If failed → increment retry, loop back
  4. If all attempts exhausted → mark FAILED

Each step.run() is memoized by Inngest — if the process crashes and restarts,
completed steps are skipped automatically.
"""

from inngest import Context, TriggerEvent
from sqlalchemy import select
import os

from app.inngest_client import inngest_client
from app.database import SessionLocal
from app import models
from app.models import CaseStatus
from app.domain import (
    RecoveryCaseContext, DiagnosisResult, DecisionResult,
    PolicyEvaluationResult, ExecutionResult, ExecutionStatus,
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
    )
    async def process_case_workflow(ctx: Context, step):
        return await inner_process_case_workflow(ctx, step)
    return process_case_workflow


async def inner_process_case_workflow(ctx, step):
    """Main workflow logic — exposed for testing."""
    case_id = ctx.event.data["case_id"]
    max_attempts = PolicyConfig.MAX_RETRIES + 1  # initial try + retries

    # ── Helper closures ──────────────────────────────────────────────

    def _load_case():
        db = SessionLocal()
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
        db = SessionLocal()
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

        # Stopping Rule A: Max 30-day pursuit window
        if case_domain.created_at:
            from datetime import datetime, timezone
            days_pursued = (datetime.now(timezone.utc) - case_domain.created_at.replace(tzinfo=timezone.utc if case_domain.created_at.tzinfo is None else case_domain.created_at.tzinfo)).days
            if days_pursued > 30:
                updated = await step.run(f"max_days_stop_{attempt}", lambda: _set_status(CaseStatus.CLOSED))
                if not updated:
                    return {"status": "skipped", "reason": "Case already in terminal state"}
                return {"status": "closed", "reason": f"Max pursuit period (30 days) exceeded after {days_pursued} days."}

        # Stopping Rule B: Customer opt-out
        payload = case_domain.raw_signal_payload or {}
        if payload.get("customer_opted_out") or payload.get("opt_out"):
            updated = await step.run(f"opt_out_stop_{attempt}", lambda: _set_status(CaseStatus.CLOSED))
            if not updated:
                return {"status": "skipped", "reason": "Case already in terminal state"}
            return {"status": "closed", "reason": "Customer has opted out of recovery communications."}

        # Stopping Rule C: Promise-to-Pay — if customer has committed to a date, respect it
        if case_domain.promise_to_pay_date:
            from datetime import datetime, timezone, date as date_type
            ptp_date = case_domain.promise_to_pay_date
            today = datetime.now(timezone.utc).date()

            if ptp_date > today:
                # Promise is still in the future — suppress AI loop, sleep until the committed date
                hours_to_wait = max(1, (ptp_date - today).days * 24)
                db_ptp = SessionLocal()
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
                if woken.status in [CaseStatus.RECOVERED, CaseStatus.FAILED, CaseStatus.CLOSED]:
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
            # High-value cases needing human sign-off → escalate
            if policy_eval.requires_human_approval:
                def _persist_pending(c_id=case_id, dec=decision, diag=diagnosis):
                    db_sess = SessionLocal()
                    try:
                        from app.models import RecoveryCase
                        db_sess.query(RecoveryCase).filter(RecoveryCase.id == c_id).update({
                            "pending_decision_json": dec.model_dump(mode='json'),
                            "pending_diagnosis_json": diag.model_dump(mode='json')
                        })
                        db_sess.commit()
                    finally:
                        db_sess.close()
                
                await step.run(f"persist_pending_{attempt}", _persist_pending)
                
                updated = await step.run(f"escalate_{attempt}",
                                         lambda: _set_status(CaseStatus.ESCALATED))
                if not updated:
                    return {"status": "skipped", "reason": "Case already in terminal state before escalating"}
                return {"status": "escalated", "reason": policy_eval.reason}
            # Everything else (hard decline, max retries, etc.) → fail
            updated = await step.run(f"reject_{attempt}",
                                     lambda: _set_status(CaseStatus.FAILED))
            if not updated:
                return {"status": "skipped", "reason": "Case already in terminal state before rejecting"}
            return {"status": "policy_rejected", "reason": policy_eval.reason}

        # 6. Generate customer communication
        def _comms(case=case_domain, diag=diagnosis, att=attempt):
            return run_communication_step(case, diag, att + 1)

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
                updated = await step.run(f"finalize_{attempt}", lambda: _set_status(CaseStatus.CLOSED))
                if not updated:
                    return {"status": "skipped", "reason": "Case already in terminal state before closing"}
                return {"status": CaseStatus.CLOSED.value, "execution": exec_dict}
            else:
                # Wait for payment confirmation via webhook
                updated = await step.run(f"awaiting_payment_{attempt}", lambda: _set_status(CaseStatus.AWAITING_PAYMENT))
                if not updated:
                    return {"status": "skipped", "reason": "Case already in terminal state before awaiting payment"}
                
                delay_hours = decision.action_parameters.get("delay_hours", 24)
                delay_hours = max(1, min(delay_hours, 168))
                
                await step.sleep(f"wait_for_payment_{attempt}", f"{delay_hours}h")
                
                woken_case_dict = await step.run(f"load_woken_case_{attempt}", _load_case)
                woken_case = RecoveryCaseContext.model_validate(woken_case_dict)
                
                if woken_case.status in [CaseStatus.RECOVERED, CaseStatus.FAILED, CaseStatus.CLOSED]:
                    return {"status": woken_case.status.value, "execution": exec_dict}
                
                # Fall through to retry block if payment wasn't confirmed

        # 9. Execution failed — can we retry?
        if attempt < max_attempts - 1:
            updated = await step.run(f"retry_{attempt}",
                                     lambda: _set_status(CaseStatus.IN_PROGRESS, inc_retry=True))
            if not updated:
                return {"status": "skipped", "reason": "Case already in terminal state before retry"}
            continue  # loop back to diagnose with updated state

        # 10. All attempts exhausted
        updated = await step.run("finalize_failed", lambda: _set_status(CaseStatus.FAILED))
    if not updated:
        return {"status": "skipped", "reason": "Case already in terminal state"}
    return {"status": "failed", "reason": "All recovery attempts exhausted"}


@inngest_client.create_function(
    fn_id="monitor-awaiting-payment",
    name="Monitor Awaiting Payment",
    trigger=TriggerEvent(event="case.monitor_payment"),
)
async def monitor_awaiting_payment(ctx: Context, step: Context.step_api) -> dict:
    """
    Monitors a case that was placed into AWAITING_PAYMENT by a manual approval.
    Unlike the standard loop, if payment is not received after the delay, this
    workflow fails the case permanently without auto-retrying with new AI decisions.
    """
    case_id = ctx.event.data["case_id"]

    def _load_case():
        db = SessionLocal()
        try:
            from app.models import RecoveryCase
            case_orm = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
            if not case_orm:
                raise ValueError(f"Case {case_id} not found")
            return {
                "id": case_orm.id,
                "customer_id": case_orm.customer_id,
                "case_type": case_orm.case_type,
                "status": case_orm.status,
                "amount_paise": case_orm.amount_paise,
                "currency": case_orm.currency,
                "payment_rail": case_orm.payment_rail,
                "priority_score": case_orm.priority_score,
                "raw_signal_payload": case_orm.raw_signal_payload,
                "retry_count": case_orm.retry_count,
                "cumulative_discount_paise": case_orm.cumulative_discount_paise,
                "created_at": case_orm.created_at.isoformat() if case_orm.created_at else None,
                "promise_to_pay_date": case_orm.promise_to_pay_date.isoformat() if case_orm.promise_to_pay_date else None,
                "session_id": case_orm.session_id,
            }
        finally:
            db.close()

    def _set_status(new_status: CaseStatus):
        return update_case_status(case_id, new_status, ["awaiting_payment", "in_progress"])
        
    def _write_audit():
        db = SessionLocal()
        try:
            from app.models import AuditLog
            from app.db.audit_repository import _generate_audit_id
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            
            # Using -1 for retry_count to ensure uniqueness from main loop
            audit_id = _generate_audit_id(case_id, "MANUAL_MONITOR_WAIT", -1)
            stmt = pg_insert(AuditLog).values(
                id=audit_id, case_id=case_id, action_type="MANUAL_MONITOR_WAIT",
                description="Waiting for payment after manual approval",
                reasoning="Sleeping for 24h to allow customer time to pay."
            ).on_conflict_do_nothing()
            db.execute(stmt)
            db.commit()
        finally:
            db.close()

    # Sleep
    await step.run("audit_monitor_wait", _write_audit)
    delay = "24h" # Default delay, we could pass this in event.data
    await step.sleep("monitor_wait", delay)

    # Re-check
    case_dict = await step.run("reload_case", _load_case)
    if case_dict["status"] in [CaseStatus.RECOVERED.value, CaseStatus.CLOSED.value, CaseStatus.FAILED.value]:
        return {"status": case_dict["status"], "reason": "Resolved during monitoring window"}

    # Fail
    updated = await step.run("fail_monitored_case", lambda: _set_status(CaseStatus.FAILED))
    if updated:
        def _write_fail_audit():
            db = SessionLocal()
            try:
                from app.models import AuditLog
                from app.db.audit_repository import _generate_audit_id
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                
                audit_id = _generate_audit_id(case_id, "MANUAL_MONITOR_FAILED", -1)
                stmt = pg_insert(AuditLog).values(
                    id=audit_id, case_id=case_id, action_type="MANUAL_MONITOR_FAILED",
                    description="Case failed after single manual attempt",
                    reasoning="Manually-approved action not paid within window — case failed after single attempt (does not re-enter AI decision loop)"
                ).on_conflict_do_nothing()
                db.execute(stmt)
                db.commit()
            finally:
                db.close()
        await step.run("audit_monitor_fail", _write_fail_audit)
        return {"status": "failed", "reason": "Unpaid after manual approval window"}
    
    return {"status": "skipped", "reason": "Already in terminal state"}


process_case_workflow = _get_inngest_function()
