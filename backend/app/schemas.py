from pydantic import BaseModel, Field, ConfigDict
from typing import Dict, Any, Optional
from datetime import datetime, date
from app.models import CaseType, CaseStatus

class RecoveryCaseBase(BaseModel):
    case_type: CaseType
    amount_paise: int = Field(gt=0, description="Amount at risk in the smallest currency unit (e.g., paise for INR)")
    currency: str = Field(default="INR")
    customer_id: str
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
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
    cumulative_comms_cost_paise: int
    latest_diagnosis_category: Optional[str] = None
    latest_diagnosis_confidence: Optional[float] = None
    latest_diagnosis_reasoning: Optional[str] = None
    latest_action_recommended: Optional[str] = None
    latest_channel: Optional[str] = None
    latest_comms_preview: Optional[str] = None
    promise_to_pay_date: Optional[date] = None
    last_notified_at: Optional[datetime] = None
    opted_out: bool = False
    pending_decision_json: Optional[Dict[str, Any]] = None
    pending_diagnosis_json: Optional[Dict[str, Any]] = None
    approval_status: Optional[str] = None
    approved_decision_id: Optional[str] = None
    approved_decision_hash: Optional[str] = None
    session_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PromiseToPayRequest(BaseModel):
    date: date
    note: Optional[str] = None

class DecisionApprovalRequest(BaseModel):
    decision_id: Optional[str] = None
    reviewer_id: Optional[str] = None
    decision_hash: str

class OptOutRequest(BaseModel):
    reason: Optional[str] = "Customer requested communication opt-out"

class InvoiceOverdueSignalRequest(BaseModel):
    invoice_id: str
    customer_id: str
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
    amount_paise: int = Field(gt=0, description="Invoice amount in paise")
    currency: str = "INR"
    days_overdue: int = Field(default=1, ge=1)
    due_date: Optional[str] = None
    invoice_number: Optional[str] = None

class CheckoutBeaconRequest(BaseModel):
    session_id: str
    last_interaction_at: datetime
    amount_paise: int
    currency: str = "INR"
    customer_id: str
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
    cart_items: int = 1

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
    min_confidence_score: float = 0.7
    min_enach_delay_hours: int = 72
    pre_debit_notice_hours: int = 24
    max_days_pursued: int = 14

    model_config = ConfigDict(from_attributes=True)
