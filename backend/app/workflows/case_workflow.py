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
from app.ai.provider import FallbackProvider, GeminiProvider, GroqProvider
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
            update_case_status(db, case_id, new_status, increment_retry=inc_retry)
        finally:
            db.close()

    def _get_provider():
        return FallbackProvider(GeminiProvider(), GroqProvider())

    # ── Mark case as in progress ─────────────────────────────────────

    await step.run("mark_in_progress", lambda: _set_status(CaseStatus.IN_PROGRESS))

    # ── Recovery loop ────────────────────────────────────────────────

    for attempt in range(max_attempts):

        # 1. Load / reload case (picks up updated retry_count)
        case_dict = await step.run(f"load_case_{attempt}", _load_case)
        case_domain = RecoveryCaseContext.model_validate(case_dict)

        # 2. Diagnose
        def _diagnose(case=case_domain):
            db = SessionLocal()
            try:
                return run_diagnosis_step(db, case, _get_provider()).model_dump()
            finally:
                db.close()

        diagnosis_dict = await step.run(f"diagnose_{attempt}", _diagnose)
        diagnosis = DiagnosisResult.model_validate(diagnosis_dict)

        # 3. Decide
        def _decide(case=case_domain, diag=diagnosis):
            db = SessionLocal()
            try:
                return run_decision_step(db, case, diag, _get_provider()).model_dump()
            finally:
                db.close()

        decision_dict = await step.run(f"decide_{attempt}", _decide)
        decision = DecisionResult.model_validate(decision_dict)

        # 4. Policy check
        def _policy(case=case_domain, dec=decision):
            db = SessionLocal()
            try:
                return run_policy_step(db, case, dec).model_dump()
            finally:
                db.close()

        policy_dict = await step.run(f"policy_{attempt}", _policy)
        policy_eval = PolicyEvaluationResult.model_validate(policy_dict)

        # 5. Handle policy rejection
        if not policy_eval.allowed or not policy_eval.approved_decision:
            # High-value cases needing human sign-off → escalate
            if "Human approval" in policy_eval.reason:
                await step.run(f"escalate_{attempt}",
                               lambda: _set_status(CaseStatus.ESCALATED))
                return {"status": "escalated", "reason": policy_eval.reason}
            # Everything else (hard decline, max retries, etc.) → fail
            await step.run(f"reject_{attempt}",
                           lambda: _set_status(CaseStatus.FAILED))
            return {"status": "policy_rejected", "reason": policy_eval.reason}

        # 6. Generate customer communication
        def _comms(case=case_domain, diag=diagnosis, att=attempt):
            db = SessionLocal()
            try:
                return run_communication_step(db, case, diag, att + 1)
            finally:
                db.close()

        await step.run(f"comms_{attempt}", _comms)

        # 7. Execute the approved action
        approved = policy_eval.approved_decision

        def _execute(case=case_domain, appr=approved):
            from app.execution.executor import RazorpayExecutor
            db = SessionLocal()
            try:
                executor = RazorpayExecutor()
                return run_execution_step(db, case, appr, executor).model_dump()
            finally:
                db.close()

        exec_dict = await step.run(f"execute_{attempt}", _execute)
        exec_result = ExecutionResult.model_validate(exec_dict)

        # 8. Determine outcome
        action = decision.recommended_action

        if exec_result.status in [ExecutionStatus.SUCCESS, ExecutionStatus.DRY_RUN]:
            # Pick the right terminal status based on the action
            if action == RecoveryActionType.ESCALATE_TO_HUMAN:
                final_status = CaseStatus.ESCALATED
            elif action == RecoveryActionType.STOP:
                final_status = CaseStatus.CLOSED
            else:
                final_status = CaseStatus.RECOVERED

            await step.run(f"finalize_{attempt}",
                           lambda s=final_status: _set_status(s))
            return {"status": final_status.value, "execution": exec_dict}

        # 9. Execution failed — can we retry?
        if attempt < max_attempts - 1:
            await step.run(f"retry_{attempt}",
                           lambda: _set_status(CaseStatus.IN_PROGRESS, inc_retry=True))
            continue  # loop back to diagnose with updated state

        # 10. All attempts exhausted
        await step.run("mark_failed", lambda: _set_status(CaseStatus.FAILED))
        return {"status": "failed",
                "reason": "All recovery attempts exhausted",
                "execution": exec_dict}


process_case_workflow = _get_inngest_function()
