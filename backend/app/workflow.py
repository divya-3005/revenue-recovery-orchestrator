from sqlalchemy.orm import Session
from app.domain import RecoveryCaseContext, DiagnosisResult
from app.ai.provider import AIProvider
from app.ai.diagnosis import diagnose_case
from app.db.audit_repository import record_diagnosis_checkpoint

def run_diagnosis_step(session: Session, case: RecoveryCaseContext, provider: AIProvider) -> DiagnosisResult:
    """
    Executes the diagnosis step and durably checkpoints the result.
    The AI call happens OUTSIDE of any active database transaction.
    """
    # 1. AI Call (No DB Transaction held)
    diagnosis = diagnose_case(case, provider)
    
    # 2. Durable Audit Checkpoint (Short DB Transaction)
    record_diagnosis_checkpoint(session, case, diagnosis)
    
    return diagnosis
