import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from app import models, schemas
from app.database import engine, get_db
from app.policy import PolicyConfig

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure tables are created (in a real prod app, use Alembic, but this works for local init)
    logger.info("Creating database tables if they do not exist...")
    models.Base.metadata.create_all(bind=engine)
    yield
    logger.info("Shutting down application...")

app = FastAPI(
    title="Revenue Recovery Orchestrator API",
    description="API for ingesting and managing revenue recovery cases.",
    version="0.1.0",
    lifespan=lifespan
)

@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    return {"status": "healthy"}

@app.post("/api/v1/cases", response_model=schemas.RecoveryCaseResponse, status_code=status.HTTP_201_CREATED)
def create_case(case_in: schemas.RecoveryCaseCreate, db: Session = Depends(get_db)):
    """
    Ingest a new risk signal and normalize it into a Recovery Case.
    """
    db_case = models.RecoveryCase(
        case_type=case_in.case_type,
        amount_paise=case_in.amount_paise,
        currency=case_in.currency,
        customer_id=case_in.customer_id,
        payment_rail=case_in.payment_rail,
        raw_signal_payload=case_in.raw_signal_payload,
    )
    
    # Create the initial audit log immediately and atomically append it
    audit_log = models.AuditLog(
        action_type="SIGNAL_RECEIVED",
        description=f"Case created for signal type: {case_in.case_type.value}",
        reasoning="Initial ingestion from external signal"
    )
    db_case.audit_logs.append(audit_log)

    db.add(db_case)
    db.commit()
    db.refresh(db_case)
    logger.info(f"Created RecoveryCase {db_case.id} for customer {db_case.customer_id} with initial audit log")
    return db_case

@app.get("/api/v1/cases", response_model=List[schemas.RecoveryCaseResponse])
def list_cases(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    cases = db.query(models.RecoveryCase).offset(skip).limit(limit).all()
    return cases
@app.get("/api/v1/policy", response_model=schemas.PolicyConfigResponse)
def get_policy():
    return schemas.PolicyConfigResponse(
        max_retries=PolicyConfig.MAX_RETRIES,
        max_discount_percent=PolicyConfig.MAX_DISCOUNT_PERCENT,
        require_human_approval_above_paise=PolicyConfig.REQUIRE_HUMAN_APPROVAL_ABOVE_PAISE,
        block_hard_declines=PolicyConfig.BLOCK_HARD_DECLINES
    )
