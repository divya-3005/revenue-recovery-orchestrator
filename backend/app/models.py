from sqlalchemy import Column, String, Integer, Float, DateTime, Date, Enum, ForeignKey, JSON, Boolean
from sqlalchemy.dialects.postgresql import JSONB

# Use JSONB for Postgres, fallback to JSON for SQLite (tests)
JsonType = JSON().with_variant(JSONB, 'postgresql')
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base
import enum
import uuid

class CaseType(str, enum.Enum):
    SUBSCRIPTION_FAILED = "subscription_failed"
    CHECKOUT_ABANDONED = "checkout_abandoned"
    INVOICE_OVERDUE = "invoice_overdue"

class CaseStatus(str, enum.Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_PAYMENT = "awaiting_payment"  # Deprecating conceptually, keeping for backward compat during migration
    PAYMENT_PENDING = "payment_pending"
    PARTIALLY_RECOVERED = "partially_recovered"
    RECOVERED = "recovered"
    FAILED = "failed"
    ESCALATED = "escalated"
    CLOSED = "closed"

class ApprovalStatus(str, enum.Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

def generate_uuid():
    return str(uuid.uuid4())

from sqlalchemy import Index, text

class RecoveryCase(Base):
    __tablename__ = "recovery_cases"
    __table_args__ = (
        Index(
            "ix_recovery_cases_razorpay_event_id", 
            "razorpay_event_id", 
            unique=True, 
            postgresql_where=text("razorpay_event_id IS NOT NULL")
        ),
        Index(
            "ix_recovery_cases_session_id", 
            "session_id", 
            unique=True, 
            postgresql_where=text("session_id IS NOT NULL")
        ),
    )

    id = Column(String, primary_key=True, default=generate_uuid)
    razorpay_event_id = Column(String(255), nullable=True)
    case_type = Column(Enum(CaseType), nullable=False, index=True)
    status = Column(Enum(CaseStatus), nullable=False, default=CaseStatus.OPEN, index=True)
    amount_paise = Column(Integer, nullable=False) # Store in smallest currency unit
    currency = Column(String, nullable=False, default="INR")
    customer_id = Column(String, nullable=False, index=True)
    customer_email = Column(String(255), nullable=True)
    customer_phone = Column(String(50), nullable=True)
    payment_rail = Column(String, nullable=True) # e.g., 'card', 'upi', 'enach'
    priority_score = Column(Integer, nullable=False, default=0, index=True) # Expected Value = amount x probability
    
    raw_signal_payload = Column(JsonType, nullable=False) # Retain source data for auditability

    # Pending AI State (Feature 15)
    pending_decision_json = Column(JsonType, nullable=True)
    pending_diagnosis_json = Column(JsonType, nullable=True)
    pending_decision_id = Column(String(255), nullable=True)
    pending_decision_hash = Column(String(255), nullable=True)

    # Human Approval Gate (P0 Fix)
    approval_status = Column(Enum(ApprovalStatus), nullable=False, default=ApprovalStatus.NOT_REQUIRED)
    approved_decision_id = Column(String(255), nullable=True)
    approved_decision_hash = Column(String(255), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    approved_by = Column(String(255), nullable=True)

    # Recovery Analytics (P0 Fix)
    recovered_amount_paise = Column(Integer, nullable=False, default=0)
    payment_confirmed_at = Column(DateTime(timezone=True), nullable=True)

    # UI Visibility Cache (Latest AI Pipeline Outputs)
    latest_diagnosis_category = Column(String(50), nullable=True)
    latest_diagnosis_confidence = Column(Float, nullable=True)
    latest_diagnosis_reasoning = Column(String(500), nullable=True)
    latest_action_recommended = Column(String(50), nullable=True)
    latest_channel = Column(String(50), nullable=True)
    latest_comms_preview = Column(String(500), nullable=True)

    # Idempotency for Checkouts (Feature 1)
    session_id = Column(String(255), nullable=True)

    # Execution State
    retry_count = Column(Integer, nullable=False, default=0)
    cumulative_discount_paise = Column(Integer, nullable=False, default=0)
    cumulative_comms_cost_paise = Column(Integer, nullable=False, default=0)
    last_notified_at = Column(DateTime(timezone=True), nullable=True)
    opted_out = Column(Boolean, nullable=False, default=False)

    # Promise-to-Pay (Feature 14)
    promise_to_pay_date = Column(Date, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
    audit_logs = relationship("AuditLog", back_populates="case", cascade="all, delete-orphan")

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(String, primary_key=True, default=generate_uuid)
    case_id = Column(String, ForeignKey("recovery_cases.id"), nullable=False, index=True)
    action_type = Column(String, nullable=False, index=True)
    description = Column(String, nullable=False)
    reasoning = Column(String, nullable=True)
    
    # Append-only: No onupdate
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    case = relationship("RecoveryCase", back_populates="audit_logs")

class PaymentConfirmation(Base):
    __tablename__ = "payment_confirmations"

    id = Column(String, primary_key=True, default=generate_uuid)
    payment_id = Column(String, nullable=False, unique=True, index=True)
    case_id = Column(String, ForeignKey("recovery_cases.id"), nullable=False, index=True)
    amount_paise = Column(Integer, nullable=False)
    source = Column(String, nullable=False, default="webhook")
    confirmed_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    case = relationship("RecoveryCase")
