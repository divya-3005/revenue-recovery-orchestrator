from pydantic import BaseModel, Field, ConfigDict
from typing import Dict, Any, Optional
from datetime import datetime, date
from app.models import CaseType, CaseStatus

class RecoveryCaseBase(BaseModel):
    case_type: CaseType
    amount_paise: int = Field(gt=0, description="Amount at risk in the smallest currency unit (e.g., paise for INR)")
    currency: str = Field(default="INR")
    customer_id: str
    payment_rail: Optional[str] = None
    raw_signal_payload: Dict[str, Any]

class RecoveryCaseCreate(RecoveryCaseBase):
    pass

class RecoveryCaseResponse(RecoveryCaseBase):
    id: str
    status: CaseStatus
    priority_score: int
    retry_count: int
    cumulative_discount_paise: int
    promise_to_pay_date: Optional[date] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PromiseToPayRequest(BaseModel):
    date: date
    note: Optional[str] = None

class AuditLogResponse(BaseModel):
    id: str
    case_id: str
    action_type: str
    description: str
    reasoning: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
class PolicyConfigResponse(BaseModel):
    max_retries: int
    max_discount_percent: int
    require_human_approval_above_paise: int
    block_hard_declines: bool

    model_config = ConfigDict(from_attributes=True)
