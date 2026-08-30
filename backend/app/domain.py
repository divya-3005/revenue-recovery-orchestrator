from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Any, Dict, Optional
from datetime import datetime, date
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
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
    payment_rail: Optional[str] = None
    priority_score: int = 0
    raw_signal_payload: Dict[str, Any]
    created_at: Optional[datetime] = None
    promise_to_pay_date: Optional[date] = None
    # Pending AI State (Feature 15)
    pending_decision_json: Optional[Dict[str, Any]] = None
    pending_diagnosis_json: Optional[Dict[str, Any]] = None
    pending_decision_id: Optional[str] = None
    pending_decision_hash: Optional[str] = None
    session_id: Optional[str] = None
    
    # Human Approval Gate
    approval_status: str = "not_required"
    approved_decision_id: Optional[str] = None
    approved_decision_hash: Optional[str] = None

    # UI Visibility Cache & Trace
    latest_diagnosis_category: Optional[str] = None
    latest_diagnosis_confidence: Optional[float] = None
    latest_diagnosis_reasoning: Optional[str] = None
    latest_action_recommended: Optional[str] = None
    latest_channel: Optional[str] = None
    latest_comms_preview: Optional[str] = None
    customer_payment_history: Optional[Dict[str, Any]] = None

    # Execution State
    retry_count: int
    cumulative_discount_paise: int
    last_notified_at: Optional[datetime] = None
    opted_out: bool = False

    # We allow ORM mapping so we can easily convert from SQLAlchemy models
    model_config = ConfigDict(from_attributes=True)

class RecoveryActionType(str, enum.Enum):
    RETRY_CHARGE = "retry_charge"
    CREATE_PAYMENT_LINK = "create_payment_link"
    SEND_REMINDER = "send_reminder"
    OFFER_DISCOUNT = "offer_discount"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    STOP = "stop"
    SWITCH_RAIL = "switch_rail"

class DecisionResult(BaseModel):
    recommended_action: RecoveryActionType
    action_parameters: Dict[str, Any] = Field(default_factory=dict)
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    reasoning: str
    requires_human_approval: bool = False
    decision_id: str = ""
    decision_hash: str = ""

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
        elif self.recommended_action == RecoveryActionType.CREATE_PAYMENT_LINK:
            if "delay_hours" not in self.action_parameters:
                self.action_parameters["delay_hours"] = 0
        elif self.recommended_action == RecoveryActionType.SEND_REMINDER:
            if "channel" not in self.action_parameters:
                raise ValueError("SEND_REMINDER requires 'channel' parameter")
            if self.action_parameters["channel"] not in ["email", "sms", "whatsapp"]:
                raise ValueError("'channel' must be 'email', 'sms', or 'whatsapp'")
        elif self.recommended_action == RecoveryActionType.SWITCH_RAIL:
            if "target_rail" not in self.action_parameters:
                self.action_parameters["target_rail"] = "upi"
            valid_rails = ["upi", "card", "netbanking", "enach", "emandate", "nach"]
            if self.action_parameters["target_rail"].lower() not in valid_rails:
                raise ValueError(f"'target_rail' must be one of {valid_rails}")
            if "channel" not in self.action_parameters:
                self.action_parameters["channel"] = "email"

        if not self.decision_id:
            self.decision_id = self._derive_decision_id()
        if not self.decision_hash:
            self.decision_hash = self.canonical_hash()
        return self

    def canonical_payload(self) -> Dict[str, Any]:
        return {
            "recommended_action": self.recommended_action.value,
            "action_parameters": self.action_parameters,
            "reasoning": self.reasoning,
            "requires_human_approval": self.requires_human_approval,
        }

    def canonical_json(self) -> str:
        """Returns a deterministic compact JSON representation for approval binding."""
        import json
        return json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))

    def canonical_hash(self) -> str:
        """Returns the SHA-256 hash of the canonical decision payload."""
        import hashlib
        return hashlib.sha256(self.canonical_json().encode('utf-8')).hexdigest()

    def _derive_decision_id(self) -> str:
        import hashlib
        payload = self.canonical_json()
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]

class ExecutionStatus(str, enum.Enum):
    DRY_RUN = "dry_run"
    SUCCESS = "success"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"

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
    requires_human_approval: bool = False


class PolicyEvaluationResult(BaseModel):
    allowed: bool
    reason: str
    approved_decision: Optional[PolicyApprovedDecision] = None
    requires_human_approval: bool = False
    rules: list[Dict[str, Any]] = Field(default_factory=list)


class PipelineResult(BaseModel):
    diagnosis: DiagnosisResult
    decision: DecisionResult
    policy_evaluation: PolicyEvaluationResult
    execution_result: Optional[ExecutionResult] = None
