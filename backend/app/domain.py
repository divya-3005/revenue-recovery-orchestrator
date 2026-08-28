from pydantic import BaseModel, ConfigDict, Field
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
    raw_signal_payload: Dict[str, Any]
    
    # Execution State
    retry_count: int
    cumulative_discount_paise: int
    active_diagnosis: Optional[DiagnosisResult] = None
    
    # We allow ORM mapping so we can easily convert from SQLAlchemy models
    model_config = ConfigDict(from_attributes=True)
