"""
Audit Repository — durable checkpoint persistence.

Every checkpoint uses a deterministic ID based on (case_id, stage, retry_count)
so that replays and retries produce the same ID and are safely deduplicated
via PostgreSQL ON CONFLICT DO NOTHING.
"""

import hashlib
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import update
from app.models import AuditLog, RecoveryCase, CaseStatus
from app.domain import RecoveryCaseContext, DiagnosisResult, ExecutionStatus, RecoveryActionType


def _generate_audit_id(case_id: str, stage: str, logical_attempt: int) -> str:
    """
    Deterministic audit ID = sha256(case_id + stage + logical_attempt).
    Safe across process restarts because logical_attempt comes from durable DB state.
    """
    raw = f"{case_id}-{stage}-{logical_attempt}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


# ── Checkpoint writers ───────────────────────────────────────────────────

def record_diagnosis_checkpoint(session: Session, case: RecoveryCaseContext, diagnosis: DiagnosisResult) -> bool:
    audit_id = _generate_audit_id(case.id, "DIAGNOSIS_COMPLETED", case.retry_count)
    stmt = insert(AuditLog).values(
        id=audit_id,
        case_id=case.id,
        action_type="DIAGNOSIS_COMPLETED",
        description=f"Diagnosis completed with category: {diagnosis.root_cause_category.value}",
        reasoning=diagnosis.reasoning
    ).on_conflict_do_nothing().returning(AuditLog.id)
    result = session.execute(stmt)
    inserted_id = result.scalar()
    
    if inserted_id is not None:
        db_case = session.query(RecoveryCase).filter(RecoveryCase.id == case.id).first()
        if db_case:
            db_case.latest_diagnosis_category = diagnosis.root_cause_category.value
            db_case.latest_diagnosis_reasoning = diagnosis.reasoning

    session.commit()
    return inserted_id is not None


def record_decision_checkpoint(session: Session, case: RecoveryCaseContext, decision) -> bool:
    audit_id = _generate_audit_id(case.id, "DECISION_PROPOSED", case.retry_count)
    import json
    stmt = insert(AuditLog).values(
        id=audit_id,
        case_id=case.id,
        action_type="DECISION_PROPOSED",
        description=f"AI proposed action: {decision.recommended_action.value}",
        reasoning=json.dumps(decision.model_dump(mode='json'))
    ).on_conflict_do_nothing().returning(AuditLog.id)
    result = session.execute(stmt)
    inserted_id = result.scalar()
    
    if inserted_id is not None:
        db_case = session.query(RecoveryCase).filter(RecoveryCase.id == case.id).first()
        if db_case:
            db_case.latest_action_recommended = decision.recommended_action.value

    session.commit()
    return inserted_id is not None


def record_policy_checkpoint(session: Session, case: RecoveryCaseContext, policy_eval) -> bool:
    audit_id = _generate_audit_id(case.id, "POLICY_EVALUATED", case.retry_count)
    import json
    stmt = insert(AuditLog).values(
        id=audit_id,
        case_id=case.id,
        action_type="POLICY_EVALUATED",
        description=f"Policy {'APPROVED' if policy_eval.allowed else 'REJECTED'}: {policy_eval.reason}",
        reasoning=json.dumps({"allowed": policy_eval.allowed, "rules": policy_eval.rules})
    ).on_conflict_do_nothing().returning(AuditLog.id)
    result = session.execute(stmt)
    session.commit()
    return result.scalar() is not None


def record_communication_checkpoint(session: Session, case: RecoveryCaseContext, message: str) -> bool:
    audit_id = _generate_audit_id(case.id, "COMMUNICATION_SENT", case.retry_count)
    stmt = insert(AuditLog).values(
        id=audit_id,
        case_id=case.id,
        action_type="COMMUNICATION_SENT",
        description=f"Recovery message generated (attempt {case.retry_count + 1})",
        reasoning=message
    ).on_conflict_do_nothing().returning(AuditLog.id)
    result = session.execute(stmt)
    inserted_id = result.scalar()
    
    if inserted_id is not None:
        db_case = session.query(RecoveryCase).filter(RecoveryCase.id == case.id).first()
        if db_case:
            db_case.latest_comms_preview = message
            # Assuming a standard comms cost of ₹0.25 (25 paise) per message
            db_case.cumulative_comms_cost_paise += 25

    session.commit()
    return inserted_id is not None


def record_execution_checkpoint(session: Session, case: RecoveryCaseContext, exec_result) -> bool:
    audit_id = _generate_audit_id(case.id, "ACTION_EXECUTED", case.retry_count)
    stmt = insert(AuditLog).values(
        id=audit_id,
        case_id=case.id,
        action_type="ACTION_EXECUTED",
        description=f"Execution {exec_result.status.value}: {exec_result.action_taken.value}",
        reasoning=exec_result.reason
    ).on_conflict_do_nothing().returning(AuditLog.id)
    result = session.execute(stmt)
    inserted_id = result.scalar()
    
    # Update cumulative discount if applicable, only if the execution log was actually inserted.
    # This prevents double-counting if Inngest replays an already-completed step.
    if inserted_id is not None and exec_result.status == ExecutionStatus.SUCCESS and exec_result.action_taken == RecoveryActionType.OFFER_DISCOUNT:
        discount_applied = exec_result.action_parameters_used.get("discount_applied_paise", 0)
        if discount_applied > 0:
            db_case = session.query(RecoveryCase).filter(RecoveryCase.id == case.id).first()
            if db_case:
                db_case.cumulative_discount_paise += discount_applied

    session.commit()
    return inserted_id is not None


# ── Case status helpers ──────────────────────────────────────────────────

def update_case_status(session: Session, case_id: str, new_status, increment_retry: bool = False) -> bool:
    """Update the case status in the database. Optionally increment retry_count. Returns True if updated."""
    values = {"status": new_status}
    if increment_retry:
        values["retry_count"] = RecoveryCase.retry_count + 1
        
    stmt = update(RecoveryCase).where(
        RecoveryCase.id == case_id,
        RecoveryCase.status.notin_([CaseStatus.RECOVERED, CaseStatus.FAILED, CaseStatus.CLOSED])
    ).values(**values).returning(RecoveryCase.id)
    
    result = session.execute(stmt)
    updated_id = result.scalar()
    session.commit()
    return updated_id is not None
