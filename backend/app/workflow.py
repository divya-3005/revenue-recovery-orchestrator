"""
Workflow step wrappers.

Each wrapper follows the same pattern:
  1. Run the operation (AI call or pure function) — NO DB transaction held
  2. Persist a durable audit checkpoint — short DB transaction
  3. Return the result
"""

from sqlalchemy.orm import Session
from app.domain import RecoveryCaseContext, DiagnosisResult, DecisionResult, PolicyEvaluationResult
from app.ai.provider import AIProvider
from app.ai.diagnosis import diagnose_case
from app.ai.decision import decide_action
from app.policy import evaluate_policy
from app.comms import generate_message
from app.db.audit_repository import (
    record_diagnosis_checkpoint,
    record_decision_checkpoint,
    record_policy_checkpoint,
    record_communication_checkpoint,
    record_execution_checkpoint,
)


def run_diagnosis_step(session: Session, case: RecoveryCaseContext, provider: AIProvider) -> DiagnosisResult:
    """Diagnose → checkpoint."""
    diagnosis = diagnose_case(case, provider)
    record_diagnosis_checkpoint(session, case, diagnosis)
    return diagnosis


def run_decision_step(session: Session, case: RecoveryCaseContext, diagnosis: DiagnosisResult, provider: AIProvider) -> DecisionResult:
    """Decide → checkpoint."""
    decision = decide_action(case, diagnosis, provider)
    record_decision_checkpoint(session, case, decision)
    return decision


def run_policy_step(session: Session, case: RecoveryCaseContext, decision: DecisionResult, diagnosis: DiagnosisResult) -> PolicyEvaluationResult:
    """Policy eval → checkpoint."""
    policy_eval = evaluate_policy(case, decision, diagnosis)
    record_policy_checkpoint(session, case, policy_eval)
    return policy_eval


def run_communication_step(session: Session, case: RecoveryCaseContext, diagnosis: DiagnosisResult, attempt_number: int) -> str:
    """Generate customer message → checkpoint."""
    message = generate_message(case, diagnosis, attempt_number)
    record_communication_checkpoint(session, case, message)
    return message


def run_execution_step(session: Session, case: RecoveryCaseContext, approved_decision, executor):
    """Execute action → checkpoint."""
    exec_result = executor.execute(case, approved_decision)
    record_execution_checkpoint(session, case, exec_result)
    return exec_result
