from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Any, Dict, Optional
import enum
from app.models import CaseType, CaseStatus

class RootCauseCategory(str, enum.Enum):
    HARD_DECLINE = "hard_decline"
    SOFT_DECLINE = "soft_decline"
    FRICTION = "friction"
    DISPUTE = "dispute"
    MISSED_PAYMENT = "missed_payment"
    UNKNOWN = "unknown"

class DiagnosisResult(BaseModel):
    root_cause_category: RootCauseCategory
    specific_reason: str
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    reasoning: str

class RecoveryCaseContext(BaseModel):
    """
    Pure Python domain model representing the current execution context of a Recovery Case.
    This safely decouples the business logic (policy, AI, execution) from the Database ORM.
    """
    id: str
    case_type: CaseType
    status: CaseStatus
    amount_paise: int
    currency: str
    customer_id: str
    payment_rail: str | None = None
    priority_score: int = 0
    raw_signal_payload: Dict[str, Any]
    
    # Execution State
    retry_count: int
    cumulative_discount_paise: int
    active_diagnosis: Optional[DiagnosisResult] = None
    
    # We allow ORM mapping so we can easily convert from SQLAlchemy models
    model_config = ConfigDict(from_attributes=True)

class RecoveryActionType(str, enum.Enum):
    RETRY_CHARGE = "retry_charge"
    SEND_REMINDER = "send_reminder"
    OFFER_DISCOUNT = "offer_discount"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    STOP = "stop"

class DecisionResult(BaseModel):
    recommended_action: RecoveryActionType
    action_parameters: Dict[str, Any] = Field(default_factory=dict)
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    reasoning: str

    @model_validator(mode='after')
    def validate_parameters(self) -> 'DecisionResult':
        if self.recommended_action == RecoveryActionType.OFFER_DISCOUNT:
            if "discount_percent" not in self.action_parameters:
                raise ValueError("OFFER_DISCOUNT requires 'discount_percent' parameter")
            if not isinstance(self.action_parameters["discount_percent"], int):
                raise ValueError("'discount_percent' must be an integer")
        elif self.recommended_action == RecoveryActionType.RETRY_CHARGE:
            if "delay_hours" not in self.action_parameters:
                raise ValueError("RETRY_CHARGE requires 'delay_hours' parameter")
            if not isinstance(self.action_parameters["delay_hours"], int):
                raise ValueError("'delay_hours' must be an integer")
        elif self.recommended_action == RecoveryActionType.SEND_REMINDER:
            if "channel" not in self.action_parameters:
                raise ValueError("SEND_REMINDER requires 'channel' parameter")
            if self.action_parameters["channel"] not in ["email", "sms"]:
                raise ValueError("'channel' must be 'email' or 'sms'")
        return self
class ExecutionStatus(str, enum.Enum):
    DRY_RUN = "dry_run"
    SUCCESS = "success"
    FAILED = "failed"

class ExecutionResult(BaseModel):
    status: ExecutionStatus
    action_taken: RecoveryActionType
    reason: str
    external_reference_id: Optional[str] = None
    action_parameters_used: Dict[str, Any] = Field(default_factory=dict)

class PolicyApprovedDecision(BaseModel):
    decision: DecisionResult
    policy_reason: str
    idempotency_key: str

class PolicyEvaluationResult(BaseModel):
    allowed: bool
    reason: str
    approved_decision: Optional[PolicyApprovedDecision] = None

class PipelineResult(BaseModel):
    diagnosis: DiagnosisResult
    decision: DecisionResult
    policy_evaluation: PolicyEvaluationResult
    execution_result: Optional[ExecutionResult] = None
