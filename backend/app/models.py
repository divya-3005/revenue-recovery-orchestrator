from sqlalchemy import Column, String, Integer, DateTime, Enum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
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
    RECOVERED = "recovered"
    FAILED = "failed"
    ESCALATED = "escalated"
    CLOSED = "closed"

def generate_uuid():
    return str(uuid.uuid4())

class RecoveryCase(Base):
    __tablename__ = "recovery_cases"

    id = Column(String, primary_key=True, default=generate_uuid)
    case_type = Column(Enum(CaseType), nullable=False, index=True)
    status = Column(Enum(CaseStatus), nullable=False, default=CaseStatus.OPEN, index=True)
    amount_paise = Column(Integer, nullable=False) # Store in smallest currency unit
    currency = Column(String, nullable=False, default="INR")
    customer_id = Column(String, nullable=False, index=True)
    payment_rail = Column(String, nullable=True) # e.g., 'card', 'upi', 'enach'
    
    raw_signal_payload = Column(JSONB, nullable=False) # Retain source data for auditability

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
