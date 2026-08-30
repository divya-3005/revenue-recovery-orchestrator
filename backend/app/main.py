"""
Revenue Recovery Orchestrator — FastAPI application.

Endpoints:
  POST /api/v1/cases          — Ingest a new recovery case
  GET  /api/v1/cases          — List all cases
  GET  /api/v1/cases/{id}/audit — Full audit trail for one case
  GET  /api/v1/cases/escalated — Cases needing human review
  POST /api/v1/batch          — Generate & process 50+ synthetic cases
  GET  /api/v1/analytics      — Recovery analytics dashboard data
  GET  /api/v1/policy         — Current policy configuration
  POST /api/v1/demo/confirm-payment/{id} — Local demo: mark paid (not production)
  GET  /health                — Health check
"""

import logging
import random
import hmac
import hashlib
import os
import json
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, status, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import select, desc, update
from typing import List

import inngest.fast_api
from inngest import Event

from app import models, schemas
from app.models import CaseType, CaseStatus
from app.database import engine, get_db
from app.policy import PolicyConfig
from app.inngest_client import inngest_client
from app.workflows.case_workflow import process_case_workflow

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application starting up (skipping automatic table creation, using Alembic)...")
    yield
    logger.info("Shutting down application...")


app = FastAPI(
    title="Revenue Recovery Orchestrator API",
    description="API for ingesting and managing revenue recovery cases.",
    version="0.1.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

inngest.fast_api.serve(app, inngest_client, [process_case_workflow])


def compute_priority_score(case_type: CaseType, amount_paise: int, payload: dict) -> int:
    """Compute Expected Value heuristic for the UI priority column."""
    if case_type == CaseType.SUBSCRIPTION_FAILED:
        reason = payload.get("reason", "")
        # Hard declines have low expected recovery
        if reason in ["lost_card_reported", "stolen_card", "account_closed", "fraud_suspected"]:
            return int(amount_paise * 0.2)
        # Soft declines have high expected recovery
        return int(amount_paise * 0.8)
    elif case_type == CaseType.CHECKOUT_ABANDONED:
        return int(amount_paise * 0.5)
    elif case_type == CaseType.INVOICE_OVERDUE:
        # B2B invoice generally high expected recovery
        return int(amount_paise * 0.9)
    return int(amount_paise * 0.5)


def _apply_payment_confirmed(db: Session, case_id: str, source: str) -> dict:
    """Mark a case recovered only if it is still open. Never counts a payment-link create as recovered."""
    existing = db.query(models.RecoveryCase).filter(models.RecoveryCase.id == case_id).first()
    if not existing:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

    stmt = update(models.RecoveryCase).where(
        models.RecoveryCase.id == case_id,
        models.RecoveryCase.status.notin_([CaseStatus.RECOVERED, CaseStatus.FAILED, CaseStatus.CLOSED])
    ).values(
        status=CaseStatus.RECOVERED
    ).returning(models.RecoveryCase.id)

    result = db.execute(stmt)
    updated_id = result.scalar()

    if not updated_id:
        return {"status": "idempotent", "case_id": case_id}

    audit_log = models.AuditLog(
        case_id=case_id,
        action_type="PAYMENT_CONFIRMED",
        description=f"Payment confirmed via {source}",
        reasoning="Recovered only after payment confirmation — creating a payment link does not count as recovered."
    )
    db.add(audit_log)
    db.commit()
    return {"status": "recovered", "case_id": case_id}


# ── Health ───────────────────────────────────────────────────────────────

@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    return {"status": "healthy"}


# ── Demo ─────────────────────────────────────────────────────────────────

@app.post("/api/v1/demo/confirm-payment/{case_id}")
def demo_confirm_payment(case_id: str, db: Session = Depends(get_db)):
    """Local demo only: mark a case as paid to test the AWAITING_PAYMENT -> RECOVERED transition."""
    return _apply_payment_confirmed(db, case_id, "demo_endpoint")


# ── Case CRUD ────────────────────────────────────────────────────────────

@app.post("/api/v1/cases", response_model=schemas.RecoveryCaseResponse, status_code=status.HTTP_201_CREATED)
def create_case(case_in: schemas.RecoveryCaseCreate, db: Session = Depends(get_db)):
    """Ingest a new risk signal and normalize it into a Recovery Case."""
    priority_score = compute_priority_score(case_in.case_type, case_in.amount_paise, case_in.raw_signal_payload)
    
    db_case = models.RecoveryCase(
        case_type=case_in.case_type,
        amount_paise=case_in.amount_paise,
        currency=case_in.currency,
        customer_id=case_in.customer_id,
        payment_rail=case_in.payment_rail,
        priority_score=priority_score,
        raw_signal_payload=case_in.raw_signal_payload,
    )

    # Create the initial audit log atomically with the case
    audit_log = models.AuditLog(
        action_type="SIGNAL_RECEIVED",
        description=f"Case created for signal type: {case_in.case_type.value}",
        reasoning="Initial ingestion from external signal"
    )
    db_case.audit_logs.append(audit_log)
    db.add(db_case)

    # IMPORTANT: Commit the case FIRST, then fire the Inngest event.
    db.commit()
    db.refresh(db_case)

    try:
        inngest_client.send_sync(Event(name="case.received", data={"case_id": db_case.id}))
    except Exception as e:
        logger.warning(f"Inngest event dispatch failed for case {db_case.id}: {e}. Case exists in DB — can be retried.")

    logger.info(f"Created RecoveryCase {db_case.id} for customer {db_case.customer_id}")
    return db_case

@app.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request, x_razorpay_signature: str = Header(None), db: Session = Depends(get_db)):
    raw_body = await request.body()
    webhook_secret = os.getenv("RAZORPAY_WEBHOOK_SECRET")
    
    if not webhook_secret:
        logger.error("RAZORPAY_WEBHOOK_SECRET is not configured")
        raise HTTPException(status_code=500, detail="Webhook configuration error")
        
    if not x_razorpay_signature:
        raise HTTPException(status_code=400, detail="Missing signature")
        
    # Verify signature
    expected_sig = hmac.new(
        key=webhook_secret.encode('utf-8'),
        msg=raw_body,
        digestmod=hashlib.sha256
    ).hexdigest()
    
    if not hmac.compare_digest(expected_sig, x_razorpay_signature):
        raise HTTPException(status_code=400, detail="Invalid signature")
        
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
        
    event_type = payload.get("event")
    if event_type not in ("payment.failed", "subscription.pending", "payment_link.paid", "payment.captured"):
        return {"status": "ignored", "reason": "unhandled_event_type"}
        
    event_id = payload.get("id", "unknown_event_id")
    
    from sqlalchemy.dialects.postgresql import insert
    
    if event_type in ("payment_link.paid", "payment.captured"):
        if event_type == "payment_link.paid":
            case_id = payload.get("payload", {}).get("payment_link", {}).get("entity", {}).get("notes", {}).get("case_id")
        else:
            case_id = payload.get("payload", {}).get("payment", {}).get("entity", {}).get("notes", {}).get("case_id")
            
        if not case_id:
            return {"status": "ignored", "reason": "missing_case_id"}

        return _apply_payment_confirmed(db, case_id, event_type)
    
    priority_score = 0
    # Map payload to case fields for payment.failed and subscription.pending
    if event_type == "payment.failed":
        case_type = CaseType.SUBSCRIPTION_FAILED
        payment = payload.get("payload", {}).get("payment", {}).get("entity", {})
        amount_paise = payment.get("amount", 0)
        currency = payment.get("currency", "INR")
        customer_id = payment.get("customer_id") or payment.get("email") or "unknown"
        payment_rail = payment.get("method")
        priority_score = compute_priority_score(case_type, amount_paise, payload)
    elif event_type == "subscription.pending":
        case_type = CaseType.SUBSCRIPTION_FAILED
        sub = payload.get("payload", {}).get("subscription", {}).get("entity", {})
        amount_paise = sub.get("charge_at_mrr", 0)  # best effort fallback
        currency = "INR"
        customer_id = sub.get("customer_id") or "unknown"
        payment_rail = None
        priority_score = compute_priority_score(case_type, amount_paise, payload)
        
    if amount_paise <= 0:
        return {"status": "ignored", "reason": "invalid_amount"}
        
    # Atomic INSERT ON CONFLICT DO NOTHING
    stmt = insert(models.RecoveryCase).values(
        id=models.generate_uuid(),
        case_type=case_type,
        amount_paise=amount_paise,
        currency=currency,
        customer_id=customer_id,
        payment_rail=payment_rail,
        priority_score=priority_score,
        raw_signal_payload=payload,
        razorpay_event_id=event_id
    ).on_conflict_do_nothing(
        index_elements=['razorpay_event_id'],
        index_where=models.RecoveryCase.razorpay_event_id.isnot(None)
    ).returning(models.RecoveryCase.id)
    
    result = db.execute(stmt)
    inserted_id = result.scalar()
    db.commit()
    
    if inserted_id:
        inserted = True
        case_id = inserted_id
    else:
        inserted = False
        # Retrieve the pre-existing case ID
        existing_case = db.query(models.RecoveryCase).filter(
            models.RecoveryCase.razorpay_event_id == event_id
        ).first()
        case_id = existing_case.id
    
    if not inserted:
        return {"status": "idempotent", "case_id": case_id}

    # Only create audit log and fire event if it was actually created
    audit_log = models.AuditLog(
        case_id=case_id,
        action_type="SIGNAL_RECEIVED",
        description=f"Webhook case created for event: {event_type}",
        reasoning="Automated ingestion via Razorpay webhook"
    )
    db.add(audit_log)
    db.commit()

    try:
        inngest_client.send_sync(Event(name="case.received", data={"case_id": case_id}))
    except Exception as e:
        logger.warning(f"Inngest event dispatch failed for webhook case {case_id}: {e}.")

    return {"status": "created", "case_id": case_id}


@app.get("/api/v1/cases", response_model=List[schemas.RecoveryCaseResponse])
def list_cases(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    cases = db.query(models.RecoveryCase).order_by(models.RecoveryCase.priority_score.desc(), models.RecoveryCase.created_at.desc()).offset(skip).limit(limit).all()
    return cases


# ── Audit Trail (Feature 10) ────────────────────────────────────────────

@app.get("/api/v1/cases/{case_id}/audit", response_model=List[schemas.AuditLogResponse])
def get_case_audit_trail(case_id: str, db: Session = Depends(get_db)):
    """Full audit trail for a single case — signal → diagnosis → decision → policy → execution."""
    db_case = db.query(models.RecoveryCase).filter(models.RecoveryCase.id == case_id).first()
    if not db_case:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")
    logs = db.query(models.AuditLog).filter(
        models.AuditLog.case_id == case_id
    ).order_by(models.AuditLog.created_at).all()
    return logs


# ── Escalation Queue (Feature 9) ────────────────────────────────────────

@app.get("/api/v1/cases/escalated", response_model=List[schemas.RecoveryCaseResponse])
def list_escalated_cases(db: Session = Depends(get_db)):
    """Cases needing human review — full context attached."""
    cases = db.query(models.RecoveryCase).filter(
        models.RecoveryCase.status == CaseStatus.ESCALATED
    ).all()
    return cases


# ── Batch Processing (Feature 11) ───────────────────────────────────────

@app.post("/api/v1/batch")
def run_batch(db: Session = Depends(get_db)):
    """Generate 50+ synthetic cases spanning all case types and process them."""
    synthetic = _generate_synthetic_cases()
    created_ids = []

    for case_data in synthetic:
        db_case = models.RecoveryCase(
            case_type=case_data["case_type"],
            amount_paise=case_data["amount_paise"],
            currency=case_data["currency"],
            customer_id=case_data["customer_id"],
            payment_rail=case_data.get("payment_rail"),
            priority_score=case_data.get("priority_score", 0),
            raw_signal_payload=case_data["raw_signal_payload"],
        )
        audit_log = models.AuditLog(
            action_type="SIGNAL_RECEIVED",
            description=f"Batch case created: {case_data['case_type']}",
            reasoning="Synthetic batch case"
        )
        db_case.audit_logs.append(audit_log)
        db.add(db_case)
        db.commit()
        db.refresh(db_case)

        # Fire workflow event
        try:
            inngest_client.send_sync(Event(name="case.received", data={"case_id": db_case.id}))
        except Exception as e:
            logger.warning(f"Inngest dispatch failed for batch case {db_case.id}: {e}")

        created_ids.append(db_case.id)

    return {"cases_created": len(created_ids), "case_ids": created_ids}


def _generate_synthetic_cases():
    """Generate 50 synthetic cases with realistic distribution."""
    cases = []

    # 15 subscription failures — soft declines (retryable)
    soft_reasons = ["insufficient_funds", "bank_network_timeout", "card_limit_exceeded",
                    "processing_error", "temporary_hold"]
    for i in range(15):
        amount = random.choice([9900, 29900, 49900, 99900, 199900])
        payload = {"reason": random.choice(soft_reasons)}
        cases.append({
            "case_type": CaseType.SUBSCRIPTION_FAILED.value,
            "amount_paise": amount,
            "currency": "INR",
            "customer_id": f"cust_batch_{i:03d}",
            "payment_rail": random.choice(["card", "upi", "enach"]),
            "priority_score": compute_priority_score(CaseType.SUBSCRIPTION_FAILED, amount, payload),
            "raw_signal_payload": payload
        })

    # 8 subscription failures — hard declines (should NOT be retried)
    hard_reasons = ["lost_card_reported", "stolen_card", "account_closed", "fraud_suspected"]
    for i in range(8):
        amount = random.choice([9900, 29900, 49900])
        payload = {"reason": random.choice(hard_reasons)}
        cases.append({
            "case_type": CaseType.SUBSCRIPTION_FAILED.value,
            "amount_paise": amount,
            "currency": "INR",
            "customer_id": f"cust_batch_{15 + i:03d}",
            "payment_rail": "card",
            "priority_score": compute_priority_score(CaseType.SUBSCRIPTION_FAILED, amount, payload),
            "raw_signal_payload": payload
        })

    # 12 checkout abandoned — friction signals
    for i in range(12):
        amount = random.choice([19900, 49900, 99900, 249900, 499900])
        payload = {
            "cart_items": random.randint(1, 5),
            "time_on_page_sec": random.randint(30, 300)
        }
        cases.append({
            "case_type": CaseType.CHECKOUT_ABANDONED.value,
            "amount_paise": amount,
            "currency": "INR",
            "customer_id": f"cust_batch_{23 + i:03d}",
            "payment_rail": random.choice(["card", "upi"]),
            "priority_score": compute_priority_score(CaseType.CHECKOUT_ABANDONED, amount, payload),
            "raw_signal_payload": payload
        })

    # 10 invoice overdue — missed payments
    for i in range(10):
        amount = random.choice([100000, 250000, 500000, 1000000, 2500000])
        payload = {
            "days_overdue": random.randint(3, 30),
            "invoice_number": f"INV-{1000 + i}"
        }
        cases.append({
            "case_type": CaseType.INVOICE_OVERDUE.value,
            "amount_paise": amount,
            "currency": "INR",
            "customer_id": f"cust_batch_{35 + i:03d}",
            "payment_rail": None,
            "priority_score": compute_priority_score(CaseType.INVOICE_OVERDUE, amount, payload),
            "raw_signal_payload": payload
        })

    # 5 high-value cases (trigger human approval policy)
    for i in range(5):
        amount = random.choice([5500000, 7000000, 10000000])
        payload = {"reason": "insufficient_funds"}
        cases.append({
            "case_type": CaseType.SUBSCRIPTION_FAILED.value,
            "amount_paise": amount,
            "currency": "INR",
            "customer_id": f"cust_batch_{45 + i:03d}",
            "payment_rail": "enach",
            "priority_score": compute_priority_score(CaseType.SUBSCRIPTION_FAILED, amount, payload),
            "raw_signal_payload": payload
        })

    return cases


# ── Recovery Analytics (Feature 12) ─────────────────────────────────────

@app.get("/api/v1/analytics")
def get_analytics(db: Session = Depends(get_db)):
    """Aggregate recovery metrics across all cases."""
    cases = db.query(models.RecoveryCase).all()
    if not cases:
        return {
            "total_cases": 0,
            "total_at_risk_paise": 0,
            "total_recovered_paise": 0,
            "recovery_rate_percent": 0.0,
            "breakdown_by_case_type": {},
            "breakdown_by_status": {},
            "exceptions": []
        }

    total_at_risk = sum(c.amount_paise for c in cases)
    recovered_cases = [c for c in cases if c.status == CaseStatus.RECOVERED]
    recovered_amount = sum(c.amount_paise for c in recovered_cases)

    # Breakdown by case type
    by_type = {}
    for c in cases:
        t = c.case_type.value
        if t not in by_type:
            by_type[t] = {"total": 0, "recovered": 0, "at_risk_paise": 0, "recovered_paise": 0}
        by_type[t]["total"] += 1
        by_type[t]["at_risk_paise"] += c.amount_paise
        if c.status == CaseStatus.RECOVERED:
            by_type[t]["recovered"] += 1
            by_type[t]["recovered_paise"] += c.amount_paise

    # Breakdown by status
    by_status = {}
    for c in cases:
        s = c.status.value
        by_status[s] = by_status.get(s, 0) + 1

    # Exception list — cases that couldn't be recovered
    exceptions = [
        {"case_id": c.id, "status": c.status.value,
         "case_type": c.case_type.value, "amount_paise": c.amount_paise}
        for c in cases if c.status in [CaseStatus.FAILED, CaseStatus.ESCALATED]
    ]

    return {
        "total_cases": len(cases),
        "total_at_risk_paise": total_at_risk,
        "total_recovered_paise": recovered_amount,
        "recovery_rate_percent": round(recovered_amount / total_at_risk * 100, 2) if total_at_risk > 0 else 0.0,
        "breakdown_by_case_type": by_type,
        "breakdown_by_status": by_status,
        "exceptions": exceptions,
    }


# ── Policy Config ────────────────────────────────────────────────────────

@app.get("/api/v1/policy", response_model=schemas.PolicyConfigResponse)
def get_policy():
    return schemas.PolicyConfigResponse(
        max_retries=PolicyConfig.MAX_RETRIES,
        max_discount_percent=PolicyConfig.MAX_DISCOUNT_PERCENT,
        require_human_approval_above_paise=PolicyConfig.REQUIRE_HUMAN_APPROVAL_ABOVE_PAISE,
        block_hard_declines=PolicyConfig.BLOCK_HARD_DECLINES
    )
