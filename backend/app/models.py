from sqlalchemy import Column, String, Integer, DateTime, Enum, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
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
    AWAITING_PAYMENT = "awaiting_payment"
    RECOVERED = "recovered"
    FAILED = "failed"
    ESCALATED = "escalated"
    CLOSED = "closed"

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
    )

    id = Column(String, primary_key=True, default=generate_uuid)
    razorpay_event_id = Column(String(255), nullable=True)
    case_type = Column(Enum(CaseType), nullable=False, index=True)
    status = Column(Enum(CaseStatus), nullable=False, default=CaseStatus.OPEN, index=True)
    amount_paise = Column(Integer, nullable=False) # Store in smallest currency unit
    currency = Column(String, nullable=False, default="INR")
    customer_id = Column(String, nullable=False, index=True)
    payment_rail = Column(String, nullable=True) # e.g., 'card', 'upi', 'enach'
    
    raw_signal_payload = Column(JSONB, nullable=False) # Retain source data for auditability

    # Execution State
    retry_count = Column(Integer, nullable=False, default=0)
    cumulative_discount_paise = Column(Integer, nullable=False, default=0)
    active_diagnosis = Column(JSONB, nullable=True)

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
