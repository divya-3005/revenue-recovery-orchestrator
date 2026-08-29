"""
Workflow step wrappers.

Each wrapper follows the same pattern:
  1. Run the operation (AI call or pure function) — NO DB transaction held
  2. Persist a durable audit checkpoint — short DB transaction
  3. Return the result
"""

from app.domain import RecoveryCaseContext, DiagnosisResult, DecisionResult, PolicyEvaluationResult
from app.ai.provider import AIProvider
from app.ai.diagnosis import diagnose_case
from app.ai.decision import decide_action
from app.policy import evaluate_policy
from app.comms import generate_message
from app.database import SessionLocal
from app.db.audit_repository import (
    record_diagnosis_checkpoint,
    record_decision_checkpoint,
    record_policy_checkpoint,
    record_communication_checkpoint,
    record_execution_checkpoint,
)


def run_diagnosis_step(case: RecoveryCaseContext, provider: AIProvider) -> DiagnosisResult:
    """Diagnose → checkpoint."""
    diagnosis = diagnose_case(case, provider)
    db = SessionLocal()
    try:
        record_diagnosis_checkpoint(db, case, diagnosis)
    finally:
        db.close()
    return diagnosis


def run_decision_step(case: RecoveryCaseContext, diagnosis: DiagnosisResult, provider: AIProvider) -> DecisionResult:
    """Decide → checkpoint."""
    decision = decide_action(case, diagnosis, provider)
    db = SessionLocal()
    try:
        record_decision_checkpoint(db, case, decision)
    finally:
        db.close()
    return decision


def run_policy_step(case: RecoveryCaseContext, decision: DecisionResult, diagnosis: DiagnosisResult) -> PolicyEvaluationResult:
    """Policy eval → checkpoint."""
    policy_eval = evaluate_policy(case, decision, diagnosis)
    db = SessionLocal()
    try:
        record_policy_checkpoint(db, case, policy_eval)
    finally:
        db.close()
    return policy_eval


def run_communication_step(case: RecoveryCaseContext, diagnosis: DiagnosisResult, attempt_number: int) -> str:
    """Generate customer message → checkpoint."""
    message = generate_message(case, diagnosis, attempt_number)
    db = SessionLocal()
    try:
        record_communication_checkpoint(db, case, message)
    finally:
        db.close()
    return message


def run_execution_step(case: RecoveryCaseContext, approved_decision, executor):
    """Execute action → checkpoint."""
    exec_result = executor.execute(case, approved_decision)
    db = SessionLocal()
    try:
        record_execution_checkpoint(db, case, exec_result)
    finally:
        db.close()
    return exec_result
