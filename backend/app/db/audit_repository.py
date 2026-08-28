import hashlib
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.models import AuditLog
from app.domain import RecoveryCaseContext, DiagnosisResult

def _generate_audit_id(case_id: str, stage: str, attempt: int) -> str:
    """Generates a deterministic ID to enforce uniqueness at the database layer."""
    raw = f"{case_id}-{stage}-{attempt}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

def record_diagnosis_checkpoint(session: Session, case: RecoveryCaseContext, diagnosis: DiagnosisResult) -> bool:
    """
    Persists a durable audit checkpoint for a completed diagnosis.
    Returns True if a new record was written, False if it was already recorded (idempotent).
    """
    audit_id = _generate_audit_id(case.id, "DIAGNOSIS_COMPLETED", case.retry_count)
    
    log = AuditLog(
        id=audit_id,
        case_id=case.id,
        action_type="DIAGNOSIS_COMPLETED",
        description=f"Diagnosis completed with category: {diagnosis.root_cause_category.value}",
        reasoning=diagnosis.reasoning
    )
    
    session.add(log)
    try:
        session.commit()
        return True
    except IntegrityError:
        session.rollback()
        return False
