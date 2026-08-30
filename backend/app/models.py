"""
Data models — SQLAlchemy tables + Pydantic domain types.

Tables:
  - recovery_cases: The central object every feature operates on
  - audit_logs: Append-only event log per case (Feature 10)
"""

import enum
import uuid
import hashlib
import json
from datetime import datetime, date
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, ConfigDict, model_validator
from sqlalchemy import Column, String, Integer, Float, DateTime, Date, Enum, ForeignKey, JSON, Boolean
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from app.database import Base


# ── Enums ────────────────────────────────────────────────────────────────

class CaseType(str, enum.Enum):
    SUBSCRIPTION_FAILED = "subscription_failed"
    CHECKOUT_ABANDONED = "checkout_abandoned"
    INVOICE_OVERDUE = "invoice_overdue"


class CaseStatus(str, enum.Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    AWAITING_APPROVAL = "awaiting_approval"
    PAYMENT_PENDING = "payment_pending"
    RECOVERED = "recovered"
    FAILED = "failed"
    ESCALATED = "escalated"
    CLOSED = "closed"


class ApprovalStatus(str, enum.Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# ── SQLAlchemy Tables ────────────────────────────────────────────────────

def _uuid():
    return str(uuid.uuid4())


class RecoveryCase(Base):
    __tablename__ = "recovery_cases"

    id = Column(String, primary_key=True, default=_uuid)
    case_type = Column(Enum(CaseType), nullable=False, index=True)
    status = Column(Enum(CaseStatus), nullable=False, default=CaseStatus.OPEN, index=True)
    amount_paise = Column(Integer, nullable=False)
    currency = Column(String, nullable=False, default="INR")
    customer_id = Column(String, nullable=False, index=True)
    customer_email = Column(String, nullable=True)
    customer_phone = Column(String, nullable=True)
    payment_rail = Column(String, nullable=True)  # card, upi, enach, etc.
    priority_score = Column(Integer, nullable=False, default=0, index=True)
    raw_signal_payload = Column(JSON, nullable=False)

    # AI pipeline cache (for the UI to show latest diagnosis/action)
    latest_diagnosis_category = Column(String, nullable=True)
    latest_diagnosis_confidence = Column(Float, nullable=True)
    latest_diagnosis_reasoning = Column(String, nullable=True)
    latest_action_recommended = Column(String, nullable=True)
    latest_channel = Column(String, nullable=True)
    latest_comms_preview = Column(String, nullable=True)

    # Human Approval Gate (Feature 15)
    pending_decision_json = Column(JSON, nullable=True)
    pending_diagnosis_json = Column(JSON, nullable=True)
    pending_decision_id = Column(String, nullable=True)
    pending_decision_hash = Column(String, nullable=True)
    approval_status = Column(Enum(ApprovalStatus), nullable=False, default=ApprovalStatus.NOT_REQUIRED)
    approved_decision_id = Column(String, nullable=True)
    approved_decision_hash = Column(String, nullable=True)

    # Recovery tracking
    recovered_amount_paise = Column(Integer, nullable=False, default=0)
    retry_count = Column(Integer, nullable=False, default=0)
    cumulative_discount_paise = Column(Integer, nullable=False, default=0)
    cumulative_comms_cost_paise = Column(Integer, nullable=False, default=0)
    opted_out = Column(Boolean, nullable=False, default=False)

    # Promise-to-Pay (Feature 14)
    promise_to_pay_date = Column(Date, nullable=True)

    # Webhook Idempotency
    razorpay_event_id = Column(String, nullable=True, unique=True, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    audit_logs = relationship("AuditLog", back_populates="case", cascade="all, delete-orphan")


class AuditLog(Base):
    """Append-only event log — every pipeline step writes here (Feature 10)."""
    __tablename__ = "audit_logs"

    id = Column(String, primary_key=True, default=_uuid)
    case_id = Column(String, ForeignKey("recovery_cases.id"), nullable=False, index=True)
    action_type = Column(String, nullable=False, index=True)
    description = Column(String, nullable=False)
    reasoning = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    case = relationship("RecoveryCase", back_populates="audit_logs")


# ── Pydantic Domain Models ──────────────────────────────────────────────

class RootCauseCategory(str, enum.Enum):
    HARD_DECLINE = "hard_decline"
    SOFT_DECLINE = "soft_decline"
    FRICTION = "friction"
    DISPUTE = "dispute"
    MISSED_PAYMENT = "missed_payment"
    UNKNOWN = "unknown"


class DiagnosisResult(BaseModel):
    """AI diagnosis output — classifies WHY a case is at risk (Feature 2)."""
    root_cause_category: RootCauseCategory
    specific_reason: str
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    reasoning: str


class RecoveryActionType(str, enum.Enum):
    RETRY_CHARGE = "retry_charge"
    CREATE_PAYMENT_LINK = "create_payment_link"
    SEND_REMINDER = "send_reminder"
    OFFER_DISCOUNT = "offer_discount"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    STOP = "stop"
    SWITCH_RAIL = "switch_rail"


class DecisionResult(BaseModel):
    """AI decision output — the next-best recovery action (Feature 4)."""
    recommended_action: RecoveryActionType
    action_parameters: Dict[str, Any] = Field(default_factory=dict)
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    reasoning: str

    @model_validator(mode="after")
    def fill_defaults(self) -> "DecisionResult":
        """Ensure required sub-parameters exist for each action type."""
        p = self.action_parameters
        a = self.recommended_action
        if a == RecoveryActionType.OFFER_DISCOUNT:
            p.setdefault("discount_percent", 10)
        if a in (RecoveryActionType.RETRY_CHARGE, RecoveryActionType.CREATE_PAYMENT_LINK):
            p.setdefault("delay_hours", 24)
        if a == RecoveryActionType.SEND_REMINDER:
            p.setdefault("channel", "email")
        if a == RecoveryActionType.SWITCH_RAIL:
            p.setdefault("target_rail", "upi")
            p.setdefault("channel", "email")
        return self

    def canonical_json(self) -> str:
        """Deterministic JSON for approval binding."""
        payload = {
            "recommended_action": self.recommended_action.value,
            "action_parameters": self.action_parameters,
            "reasoning": self.reasoning,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def canonical_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    @property
    def decision_id(self) -> str:
        return self.canonical_hash()[:32]


class ExecutionStatus(str, enum.Enum):
    DRY_RUN = "dry_run"
    SUCCESS = "success"
    FAILED = "failed"


class ExecutionResult(BaseModel):
    """Result of executing a recovery action (Feature 5)."""
    status: ExecutionStatus
    action_taken: RecoveryActionType
    reason: str
    external_reference_id: Optional[str] = None
    action_parameters_used: Dict[str, Any] = Field(default_factory=dict)


class PolicyResult(BaseModel):
    """Output of the guardrail check (Feature 3)."""
    allowed: bool
    reason: str
    requires_human_approval: bool = False
    decision: Optional[DecisionResult] = None
    rules_checked: list[Dict[str, Any]] = Field(default_factory=list)
