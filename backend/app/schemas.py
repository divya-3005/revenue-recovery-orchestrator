"""
API request/response schemas.
"""

from datetime import datetime, date
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, ConfigDict

from app.models import CaseType, CaseStatus


# ── Request Schemas ──────────────────────────────────────────────────────

class CaseCreateRequest(BaseModel):
    case_type: CaseType
    amount_paise: int = Field(gt=0)
    currency: str = "INR"
    customer_id: str
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
    payment_rail: Optional[str] = None
    raw_signal_payload: Dict[str, Any]


class PromiseToPayRequest(BaseModel):
    date: date
    note: Optional[str] = None


class ApprovalRequest(BaseModel):
    decision_id: str
    decision_hash: str
    reviewer_id: str


class OptOutRequest(BaseModel):
    reason: Optional[str] = "Customer requested communication opt-out"


# ── Response Schemas ─────────────────────────────────────────────────────

class CaseResponse(BaseModel):
    id: str
    case_type: CaseType
    status: CaseStatus
    amount_paise: int
    currency: str
    customer_id: str
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
    payment_rail: Optional[str] = None
    priority_score: int
    raw_signal_payload: Dict[str, Any]
    retry_count: int
    cumulative_discount_paise: int
    cumulative_comms_cost_paise: int
    recovered_amount_paise: int = 0
    latest_diagnosis_category: Optional[str] = None
    latest_diagnosis_confidence: Optional[float] = None
    latest_diagnosis_reasoning: Optional[str] = None
    latest_action_recommended: Optional[str] = None
    latest_channel: Optional[str] = None
    latest_comms_preview: Optional[str] = None
    promise_to_pay_date: Optional[date] = None
    opted_out: bool = False
    pending_decision_json: Optional[Dict[str, Any]] = None
    pending_diagnosis_json: Optional[Dict[str, Any]] = None
    pending_decision_id: Optional[str] = None
    pending_decision_hash: Optional[str] = None
    approval_status: Optional[str] = None
    approved_decision_id: Optional[str] = None
    approved_decision_hash: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


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
    min_confidence_score: float
    pre_debit_notice_hours: int
    max_days_pursued: int
