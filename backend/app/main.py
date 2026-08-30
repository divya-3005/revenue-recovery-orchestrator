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
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, status, Request, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import update, func
from typing import List

import inngest.fast_api
from inngest import Event

from app import models, schemas
from app.models import CaseType, CaseStatus
from app.database import get_db
from app.policy import PolicyConfig
from app.inngest_client import inngest_client
from app.workflows.case_workflow import process_case_workflow, execute_approved_action

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

inngest.fast_api.serve(app, inngest_client, [process_case_workflow, execute_approved_action])


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


def process_payment_confirmation(db: Session, payment_id: str, case_id: str, amount_paise: int, source: str = "webhook") -> dict:
    """Atomic, idempotent payment confirmation."""
    from datetime import datetime, timezone
    from fastapi import HTTPException
    
    if amount_paise <= 0:
        raise HTTPException(status_code=400, detail="INVALID_PAYMENT_AMOUNT")

    # 1. Atomic Upsert for Idempotency
    # For SQLite tests we use a simpler strategy but for Postgres this is the production mechanism
    if db.bind.dialect.name == "sqlite":
        # SQLite fallback for tests
        existing_conf = db.query(models.PaymentConfirmation).filter(models.PaymentConfirmation.payment_id == payment_id).first()
        if existing_conf:
            if existing_conf.case_id != case_id:
                raise HTTPException(status_code=409, detail="PAYMENT_ID_ALREADY_BOUND_TO_DIFFERENT_CASE")
            return {"status": "idempotent", "reason": "payment_already_processed"}
    else:
        # Postgres Production Path
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        now = datetime.now(timezone.utc)
        stmt = pg_insert(models.PaymentConfirmation).values(
            payment_id=payment_id,
            case_id=case_id,
            amount_paise=amount_paise,
            source=source,
            confirmed_at=now
        ).on_conflict_do_nothing(index_elements=['payment_id']).returning(models.PaymentConfirmation.payment_id)
        
        result = db.execute(stmt)
        inserted_row = result.fetchone()
        
        if not inserted_row:
            existing = (
                db.query(models.PaymentConfirmation)
                .filter(models.PaymentConfirmation.payment_id == payment_id)
                .one()
            )
            if existing.case_id != case_id:
                raise HTTPException(status_code=409, detail="PAYMENT_ID_ALREADY_BOUND_TO_DIFFERENT_CASE")
            return {"status": "idempotent", "reason": "payment_already_processed"}

    # 2. Case retrieval & State Validation
    case = db.query(models.RecoveryCase).filter(models.RecoveryCase.id == case_id).first()
    if not case:
        # If we reached here on Postgres, we inserted a payment for a missing case.
        # This shouldn't happen due to FK constraints, but handle defensively.
        db.rollback()
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

    if case.status not in (CaseStatus.PAYMENT_PENDING, CaseStatus.PARTIALLY_RECOVERED):
        db.rollback()
        raise HTTPException(status_code=409, detail="CASE_NOT_AWAITING_PAYMENT")

    outstanding = case.amount_paise - case.recovered_amount_paise
    if amount_paise > outstanding:
        # Overpayment quarantine
        case.status = CaseStatus.ESCALATED
        db.commit()
        return {"status": "escalated", "reason": "overpayment"}

    # 3. SQLite explicit insert (since Postgres uses pg_insert above)
    now = datetime.now(timezone.utc)
    if db.bind.dialect.name == "sqlite":
        conf = models.PaymentConfirmation(
            payment_id=payment_id,
            case_id=case_id,
            amount_paise=amount_paise,
            source=source,
            confirmed_at=now
        )
        db.add(conf)

    # 4. Update Case
    case.recovered_amount_paise += amount_paise
    case.payment_confirmed_at = now
    
    if case.recovered_amount_paise >= case.amount_paise:
        case.status = CaseStatus.RECOVERED
    else:
        case.status = CaseStatus.PARTIALLY_RECOVERED

    # 5. Write Audit Log
    import hashlib
    audit_id = hashlib.sha256(f"{case_id}-PAYMENT_CONFIRMED-{payment_id}".encode('utf-8')).hexdigest()
    
    audit_log = models.AuditLog(
        id=audit_id,
        case_id=case_id,
        action_type="PAYMENT_CONFIRMED",
        description=f"Payment {payment_id} confirmed via {source} for {amount_paise} paise",
        reasoning=f"New status: {case.status.value}"
    )
    db.add(audit_log)
    
    # Commit everything together
    db.commit()

    return {"status": case.status.value, "case_id": case_id, "recovered": case.recovered_amount_paise}


# ── Health ───────────────────────────────────────────────────────────────

@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    return {"status": "healthy"}


# ── Demo ─────────────────────────────────────────────────────────────────

@app.post("/api/v1/demo/confirm-payment/{case_id}")
def demo_confirm_payment(case_id: str, db: Session = Depends(get_db)):
    """Local demo only: mark a case as paid to test the AWAITING_PAYMENT -> RECOVERED transition."""
    if os.getenv("ENVIRONMENT") == "production":
        raise HTTPException(status_code=403, detail="Demo endpoint is disabled in production.")
    case = db.query(models.RecoveryCase).filter(models.RecoveryCase.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    amount = case.amount_paise - case.recovered_amount_paise
    if amount <= 0:
        return {"status": "idempotent", "reason": "fully_recovered"}
    # Generate fake payment_id
    import uuid
    payment_id = f"pay_demo_{uuid.uuid4().hex[:8]}"
    return process_payment_confirmation(db, payment_id, case_id, amount, "demo_endpoint")


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

    db.add(db_case)
    db.commit()
    db.refresh(db_case)

    audit_log_1 = models.AuditLog(
        case_id=db_case.id,
        action_type="SIGNAL_RECEIVED",
        description=f"Case created for signal type: {case_in.case_type.value}",
        reasoning="Initial ingestion from external signal"
    )
    audit_log_2 = models.AuditLog(
        case_id=db_case.id,
        action_type="SIGNAL_NORMALIZED",
        description="Signal successfully normalized into Recovery Case format",
        reasoning="Ready for AI recovery pipeline"
    )
    db.add_all([audit_log_1, audit_log_2])
    db.commit()

    try:
        inngest_client.send_sync(Event(name="case.received", data={"case_id": db_case.id}))
    except Exception as e:
        logger.warning(f"Inngest event dispatch failed for case {db_case.id}: {e}. Case exists in DB — can be retried.")

    logger.info(f"Created RecoveryCase {db_case.id} for customer {db_case.customer_id}")
    return db_case

@app.post("/api/v1/cases/beacon")
def receive_checkout_beacon(body: schemas.CheckoutBeaconRequest, db: Session = Depends(get_db)):
    """
    Ingests CHECKOUT_ABANDONED signals from the frontend.
    Enforces a 15-minute idle threshold before accepting the beacon and uses session_id for idempotency.
    """
    from datetime import datetime, timezone, timedelta
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    # Enforce 15-minute idle threshold
    now = datetime.now(timezone.utc)
    # Ensure last_interaction_at is timezone-aware for comparison
    last_interaction = body.last_interaction_at
    if last_interaction.tzinfo is None:
        last_interaction = last_interaction.replace(tzinfo=timezone.utc)
        
    idle_time = now - last_interaction
    if idle_time < timedelta(minutes=15):
        return {"status": "ignored", "reason": "idle_threshold_not_met", "idle_minutes": idle_time.total_seconds() / 60}

    priority_score = compute_priority_score(
        CaseType.CHECKOUT_ABANDONED, 
        body.amount_paise, 
        {"cart_items": body.cart_items}
    )

    stmt = pg_insert(models.RecoveryCase).values(
        case_type=CaseType.CHECKOUT_ABANDONED,
        amount_paise=body.amount_paise,
        currency=body.currency,
        customer_id=body.customer_id,
        session_id=body.session_id,
        priority_score=priority_score,
        raw_signal_payload={
            "cart_items": body.cart_items,
            "last_interaction_at": body.last_interaction_at.isoformat()
        }
    ).on_conflict_do_nothing(index_elements=['session_id'])
    
    result = db.execute(stmt)
    inserted_id = result.scalar()
    db.commit()

    if inserted_id:
        case_id = inserted_id
        
        audit_log = models.AuditLog(
            case_id=case_id,
            action_type="SIGNAL_RECEIVED",
            description="Checkout abandoned beacon received",
            reasoning="Automated ingestion via client-side beacon"
        )
        db.add(audit_log)
        db.commit()

        try:
            inngest_client.send_sync(Event(name="case.received", data={"case_id": case_id}))
        except Exception as e:
            logger.warning(f"Inngest event dispatch failed for beacon case {case_id}: {e}.")

        return {"status": "created", "case_id": case_id}
    else:
        existing = db.query(models.RecoveryCase).filter(models.RecoveryCase.session_id == body.session_id).first()
        return {"status": "idempotent", "case_id": existing.id if existing else None}

@app.post("/api/v1/signals/invoice-overdue")
def receive_invoice_overdue_signal(body: schemas.InvoiceOverdueSignalRequest, db: Session = Depends(get_db)):
    """
    Ingests INVOICE_OVERDUE signals from external billing/invoice tracking systems.
    """
    priority_score = compute_priority_score(
        CaseType.INVOICE_OVERDUE,
        body.amount_paise,
        {"days_overdue": body.days_overdue, "invoice_number": body.invoice_number}
    )

    db_case = models.RecoveryCase(
        case_type=CaseType.INVOICE_OVERDUE,
        amount_paise=body.amount_paise,
        currency=body.currency,
        customer_id=body.customer_id,
        priority_score=priority_score,
        raw_signal_payload={
            "invoice_id": body.invoice_id,
            "days_overdue": body.days_overdue,
            "due_date": body.due_date,
            "invoice_number": body.invoice_number or body.invoice_id
        }
    )

    audit_log = models.AuditLog(
        action_type="SIGNAL_RECEIVED",
        description=f"Invoice overdue signal received for {body.invoice_id} ({body.days_overdue} days past due)",
        reasoning="Automated detection via external billing signal"
    )
    db_case.audit_logs.append(audit_log)
    db.add(db_case)
    db.commit()
    db.refresh(db_case)

    try:
        inngest_client.send_sync(Event(name="case.received", data={"case_id": db_case.id}))
    except Exception as e:
        logger.warning(f"Inngest event dispatch failed for invoice case {db_case.id}: {e}.")

    return {"status": "created", "case_id": db_case.id}

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
    if event_type not in ("payment.failed", "subscription.pending", "payment_link.expired", "payment_link.paid", "payment.captured", "invoice.expired", "invoice.past_due", "invoice.paid"):
        return {"status": "ignored", "reason": "unhandled_event_type"}
        
    event_id = payload.get("id", "unknown_event_id")
    
    from sqlalchemy.dialects.postgresql import insert
    
    if event_type in ("payment_link.paid", "payment.captured", "invoice.paid"):
        if event_type == "payment_link.paid":
            entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
            case_id = entity.get("notes", {}).get("case_id")
            payment_id = entity.get("id")
            amount_paise = entity.get("amount_paid", 0)
        elif event_type == "invoice.paid":
            entity = payload.get("payload", {}).get("invoice", {}).get("entity", {})
            case_id = entity.get("notes", {}).get("case_id")
            payment_id = entity.get("payment_id") or entity.get("id")
            amount_paise = entity.get("amount_paid", 0)
        else:
            entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
            case_id = entity.get("notes", {}).get("case_id")
            payment_id = entity.get("id")
            amount_paise = entity.get("amount", 0)
            
        if not case_id or not payment_id:
            return {"status": "ignored", "reason": "missing_case_id_or_payment_id"}

        return process_payment_confirmation(db, payment_id, case_id, amount_paise, event_type)
    
    priority_score = 0
    # Map payload to case fields
    if event_type == "payment.failed":
        case_type = CaseType.SUBSCRIPTION_FAILED
        payment = payload.get("payload", {}).get("payment", {}).get("entity", {})
        amount_paise = payment.get("amount", 0)
        currency = payment.get("currency", "INR")
        customer_id = payment.get("customer_id") or payment.get("email") or "unknown"
        payment_rail = payment.get("method")
        priority_score = compute_priority_score(case_type, amount_paise, {"reason": payment.get("error_reason", "")})
    elif event_type == "subscription.pending":
        case_type = CaseType.SUBSCRIPTION_FAILED
        sub = payload.get("payload", {}).get("subscription", {}).get("entity", {})
        amount_paise = sub.get("charge_at_mrr", 0)  # best effort fallback
        currency = "INR"
        customer_id = sub.get("customer_id") or "unknown"
        payment_rail = None
        priority_score = compute_priority_score(case_type, amount_paise, {})
    elif event_type in ("payment_link.expired", "invoice.expired", "invoice.past_due"):
        # Feature 1: invoice / payment link past due — Razorpay fires this when a link expires unpaid.
        case_type = CaseType.INVOICE_OVERDUE
        if "payment_link" in payload.get("payload", {}):
            link = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
            if link.get("notes", {}).get("case_id"):
                return {"status": "ignored", "reason": "internal_payment_link_expired"}
        else:
            link = payload.get("payload", {}).get("invoice", {}).get("entity", {})
            if link.get("notes", {}).get("case_id"):
                return {"status": "ignored", "reason": "internal_invoice_expired"}
        
        amount_paise = link.get("amount", 0)
        currency = link.get("currency", "INR")
        customer = link.get("customer") or {}
        customer_id = (
            customer.get("id")
            or customer.get("email")
            or "unknown"
        )
        payment_rail = None
        priority_score = compute_priority_score(
            case_type, amount_paise,
            {"days_overdue": 0, "invoice_number": link.get("reference_id", "")}
        )
        
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
        if not existing_case:
            raise HTTPException(status_code=500, detail="Concurrency conflict, please retry")
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
        models.RecoveryCase.status.in_([CaseStatus.ESCALATED, CaseStatus.AWAITING_APPROVAL])
    ).all()
    return cases


# ── Batch Processing (Feature 11) ───────────────────────────────────────

@app.post("/api/v1/batch")
def run_batch(background_tasks: BackgroundTasks, simulate: bool = False, db: Session = Depends(get_db)):
    """Generate 50+ synthetic cases spanning all case types and process them."""
    synthetic = _generate_synthetic_cases()
    # Bug 6 Fix: Sort synthetic cases by priority score descending to enforce UI queue ordering
    synthetic.sort(key=lambda x: x.get("priority_score", 0), reverse=True)
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
            if simulate:
                pass # Event not sent to Inngest if simulating
            else:
                inngest_client.send_sync(Event(name="case.received", data={"case_id": db_case.id}))
        except Exception as e:
            logger.warning(f"Inngest dispatch failed for batch case {db_case.id}: {e}")

        created_ids.append(db_case.id)

    if simulate:
        background_tasks.add_task(_simulate_batch_pipeline, created_ids)

    return {"cases_created": len(created_ids), "case_ids": created_ids}

def _simulate_batch_pipeline(case_ids: List[str]):
    """Synchronously runs the core AI pipeline steps for demo purposes."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        from app.models import RecoveryCase, CaseStatus
        from app.domain import RecoveryCaseContext, ExecutionStatus
        from app.ai.provider import AIProvider
        from app.ai.diagnosis import diagnose_failure
        from app.ai.decision import decide_action
        from app.policy import evaluate_policy
        from app.execution.executor import RazorpayExecutor
        from app.db.audit_repository import update_case_status
        import random

        import os
        from app.ai.provider import GeminiProvider, GroqProvider, AnthropicProvider, FallbackProvider
        
        def get_prov(name):
            if name == "groq": return GroqProvider()
            if name == "anthropic": return AnthropicProvider()
            return GeminiProvider()

        provider = FallbackProvider(
            get_prov(os.getenv("AI_PRIMARY_PROVIDER", "gemini").lower()),
            get_prov(os.getenv("AI_FALLBACK_PROVIDER", "groq").lower())
        )
        executor = RazorpayExecutor()
        
        for cid in case_ids:
            try:
                c = db.query(RecoveryCase).filter(RecoveryCase.id == cid).first()
                if not c: continue
                ctx = RecoveryCaseContext(
                    id=c.id, case_type=c.case_type, amount_paise=c.amount_paise,
                    currency=c.currency, customer_id=c.customer_id, payment_rail=c.payment_rail,
                    status=c.status, priority_score=c.priority_score, raw_signal_payload=c.raw_signal_payload,
                    retry_count=c.retry_count, cumulative_discount_paise=c.cumulative_discount_paise
                )
                
                # 1. Diagnose
                diag = diagnose_failure(ctx, provider)
                if not diag: continue
                
                # 2. Decide
                dec = decide_action(ctx, diag, provider)
                if not dec: continue
                
                # 3. Policy
                pol = evaluate_policy(ctx, dec, diag)
                
                # 4. Execute or Wait
                if not pol.requires_human_approval and pol.approved_decision:
                    exec_res = executor.execute(ctx, pol.approved_decision, db)
                    if exec_res.status == ExecutionStatus.SUCCESS:
                        update_case_status(db, c.id, CaseStatus.PAYMENT_PENDING)
                        # Simulate the customer paying the generated link via the unified confirmation path
                        if random.random() < 0.6:
                            pay_id = f"pay_sim_{cid.replace('-', '')[:14]}"
                            from app.main import process_payment_confirmation
                            process_payment_confirmation(db, pay_id, c.id, c.amount_paise, "batch_simulation")
                    else:
                        update_case_status(db, c.id, CaseStatus.FAILED)
                elif pol.requires_human_approval:
                    c.status = CaseStatus.AWAITING_APPROVAL
                    c.pending_decision_json = dec.model_dump()
                    db.commit()
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Simulated batch error for {cid}: {e}")

    finally:
        db.close()

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
        abandoned_hour = random.randint(0, 23)
        payload = {
            "cart_items": random.randint(1, 5),
            "time_on_page_sec": random.randint(30, 300),
            "is_repeat_customer": random.choice([True, False]),
            "abandoned_hour": abandoned_hour,
            "time_of_day": "night" if abandoned_hour in [22, 23, 0, 1, 2, 3, 4, 5] else "day"
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
    recovered_amount = sum(c.recovered_amount_paise for c in cases)
    total_discount_cost = sum(c.cumulative_discount_paise for c in cases)
    total_comms_cost = sum(c.cumulative_comms_cost_paise for c in cases)
    net_recovered_paise = max(0, recovered_amount - total_discount_cost - total_comms_cost)

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
        by_type[t]["recovered_paise"] += c.recovered_amount_paise

    # Breakdown by status
    by_status = {}
    for c in cases:
        s = c.status.value
        by_status[s] = by_status.get(s, 0) + 1

    # Breakdown by channel
    by_channel = {}
    
    # Get latest EXECUTION audit log for each case to determine channel
    from sqlalchemy import desc
    execution_logs = db.query(models.AuditLog).filter(
        models.AuditLog.action_type == "ACTION_EXECUTED"
    ).order_by(models.AuditLog.created_at.desc()).all()
    
    # We only care about the latest execution per case
    latest_exec_per_case = {}
    for log in execution_logs:
        if log.case_id not in latest_exec_per_case:
            latest_exec_per_case[log.case_id] = log

    for c in cases:
        channel = "unknown"
        if c.id in latest_exec_per_case:
            # The channel might be in the reasoning or we fallback to payment_rail
            # reasoning format: "Razorpay payment link created... Parameters: {'channel': 'sms'...}"
            # This is a bit fragile to parse from the string reasoning, but we can check if it contains 'sms' or 'whatsapp' or 'email'
            reasoning = latest_exec_per_case[c.id].reasoning or ""
            if "'channel': 'whatsapp'" in reasoning or "whatsapp" in reasoning.lower():
                channel = "whatsapp"
            elif "'channel': 'sms'" in reasoning or "sms" in reasoning.lower():
                channel = "sms"
            elif "'channel': 'email'" in reasoning or "email" in reasoning.lower():
                channel = "email"
            elif c.payment_rail:
                channel = c.payment_rail
        elif c.payment_rail:
            channel = c.payment_rail

        if channel not in by_channel:
            by_channel[channel] = {"total": 0, "recovered": 0, "at_risk_paise": 0, "recovered_paise": 0}
        
        by_channel[channel]["total"] += 1
        by_channel[channel]["at_risk_paise"] += c.amount_paise
        if c.status == CaseStatus.RECOVERED:
            by_channel[channel]["recovered"] += 1
        by_channel[channel]["recovered_paise"] += c.recovered_amount_paise

    # Exception list — cases that couldn't be recovered
    exceptions = [
        {"case_id": c.id, "status": c.status.value,
         "case_type": c.case_type.value, "amount_paise": c.amount_paise}
        for c in cases if c.status in [CaseStatus.FAILED, CaseStatus.ESCALATED, CaseStatus.CLOSED]
    ]

    return {
        "total_cases": len(cases),
        "total_at_risk_paise": total_at_risk,
        "total_recovered_paise": recovered_amount,
        "total_discount_cost_paise": total_discount_cost,
        "total_comms_cost_paise": total_comms_cost,
        "net_recovered_paise": net_recovered_paise,
        "recovery_rate_percent": round(recovered_amount / total_at_risk * 100, 2) if total_at_risk > 0 else 0.0,
        "net_recovery_rate_percent": round(net_recovered_paise / total_at_risk * 100, 2) if total_at_risk > 0 else 0.0,
        "breakdown_by_case_type": by_type,
        "breakdown_by_status": by_status,
        "breakdown_by_channel": by_channel,
        "exceptions": exceptions,
    }


# ── Policy Config ────────────────────────────────────────────────────────

@app.get("/api/v1/policy", response_model=schemas.PolicyConfigResponse)
def get_policy():
    return schemas.PolicyConfigResponse(
        max_retries=PolicyConfig.MAX_RETRIES,
        max_discount_percent=PolicyConfig.MAX_DISCOUNT_PERCENT,
        require_human_approval_above_paise=PolicyConfig.REQUIRE_HUMAN_APPROVAL_ABOVE_PAISE,
        block_hard_declines=PolicyConfig.BLOCK_HARD_DECLINES,
        min_confidence_score=PolicyConfig.MIN_CONFIDENCE_SCORE,
        min_enach_delay_hours=PolicyConfig.MIN_ENACH_DELAY_HOURS,
        pre_debit_notice_hours=PolicyConfig.PRE_DEBIT_NOTICE_HOURS,
        max_days_pursued=PolicyConfig.MAX_DAYS_PURSUED
    )


# ── Human Approval Gate (Feature 15) ─────────────────────────────────────

@app.post("/api/v1/cases/{case_id}/approve")
def approve_escalated_case(case_id: str, payload: schemas.DecisionApprovalRequest, db: Session = Depends(get_db)):
    """Human approval: Atomically unblock case and trigger execution."""
    stmt = (
        update(models.RecoveryCase)
        .where(models.RecoveryCase.id == case_id)
        .where(models.RecoveryCase.status == CaseStatus.AWAITING_APPROVAL)
        .where(models.RecoveryCase.approval_status == models.ApprovalStatus.PENDING)
        .where(models.RecoveryCase.approved_decision_hash == payload.decision_hash)
        .values(
            approval_status=models.ApprovalStatus.APPROVED,
            approved_at=func.now(),
            approved_by=(payload.reviewer_id or payload.decision_id or "human_reviewer")
        )
        .returning(models.RecoveryCase.id)
    )
    result = db.execute(stmt)
    updated_id = result.scalar()
    
    if not updated_id:
        raise HTTPException(status_code=409, detail="APPROVAL_NO_LONGER_VALID")
        
    db.commit()

    # Trigger execution workflow
    inngest_client.send_sync(Event(name="case.execute_approved", data={"case_id": case_id}))
    return {"status": "approved", "case_id": case_id}


@app.post("/api/v1/cases/{case_id}/close")
def close_escalated_case(case_id: str, db: Session = Depends(get_db)):
    """Human closure: Mark an escalated or approval-pending case as closed."""
    stmt = (
        update(models.RecoveryCase)
        .where(models.RecoveryCase.id == case_id)
        .where(models.RecoveryCase.status.in_([CaseStatus.ESCALATED, CaseStatus.AWAITING_APPROVAL]))
        .values(status=CaseStatus.CLOSED)
        .returning(models.RecoveryCase.id)
    )
    result = db.execute(stmt)
    updated_id = result.scalar()

    if not updated_id:
        raise HTTPException(status_code=400, detail="Cannot close case: not found or not in an escalated/approval-pending state.")

    db.commit()
    return {"status": "closed", "case_id": case_id}


@app.post("/api/v1/cases/{case_id}/reject")
def reject_escalated_case(case_id: str, payload: schemas.DecisionApprovalRequest, db: Session = Depends(get_db)):
    """Human rejection: Atomically reject case and return to AI loop."""
    stmt = (
        update(models.RecoveryCase)
        .where(models.RecoveryCase.id == case_id)
        .where(models.RecoveryCase.status == CaseStatus.AWAITING_APPROVAL)
        .where(models.RecoveryCase.approval_status == models.ApprovalStatus.PENDING)
        .where(models.RecoveryCase.approved_decision_hash == payload.decision_hash)
        .values(
            approval_status=models.ApprovalStatus.REJECTED,
            status=CaseStatus.IN_PROGRESS
        )
        .returning(models.RecoveryCase.id)
    )
    result = db.execute(stmt)
    updated_id = result.scalar()
    
    if not updated_id:
        raise HTTPException(status_code=400, detail="Cannot reject case: not awaiting approval, already processed, or hash mismatch.")
        
    db.commit()

    # Log rejection
    audit_log = models.AuditLog(
        case_id=case_id,
        action_type="MANUAL_REJECT",
        description="Human reviewer rejected AI proposed action",
        reasoning="Returned to AI loop."
    )
    db.add(audit_log)
    db.commit()

    # Restart workflow
    inngest_client.send_sync(Event(name="case.received", data={"case_id": case_id}))
    return {"status": "rejected", "case_id": case_id}


# ── Promise-to-Pay Capture (Feature 14) ─────────────────────────────────

@app.post("/api/v1/cases/{case_id}/promise-to-pay")
def capture_promise_to_pay(case_id: str, body: schemas.PromiseToPayRequest, db: Session = Depends(get_db)):
    """
    Capture a customer's verbal/written commitment to pay by a specific date.
    
    Effect:
    - Records the date on the case.
    - Suppresses standard AI reminders until that date (the workflow checks this field).
    - If payment is not received by the promised date, the case is auto-escalated
      with reason 'promise to pay broken'.
    """
    existing = db.query(models.RecoveryCase).filter(models.RecoveryCase.id == case_id).first()
    if not existing:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")
    if existing.status in [CaseStatus.RECOVERED, CaseStatus.CLOSED]:
        raise HTTPException(status_code=400, detail=f"Case is already resolved ({existing.status.value}); cannot set a promise-to-pay date.")

    from datetime import date
    if body.date < date.today():
        raise HTTPException(status_code=400, detail="promise_to_pay_date must be today or in the future.")

    existing.promise_to_pay_date = body.date
    audit_log = models.AuditLog(
        case_id=case_id,
        action_type="PTP_CAPTURED",
        description=f"Promise-to-pay captured: customer committed to pay by {body.date}",
        reasoning=(
            f"{body.note or 'Customer stated they will pay by this date.'} "
            f"Standard recovery reminders suppressed until {body.date}. "
            f"Automatic escalation will trigger if payment is not received by then."
        )
    )
    db.add(audit_log)
    db.commit()

    return {
        "status": "captured",
        "case_id": case_id,
        "promise_to_pay_date": str(body.date),
        "message": f"Reminders suppressed until {body.date}. Auto-escalation on breach."
    }


# ── Customer Opt-Out (Feature 8) ────────────────────────────────────────

@app.post("/api/v1/cases/{case_id}/opt-out")
def record_customer_opt_out(case_id: str, body: schemas.OptOutRequest = schemas.OptOutRequest(), db: Session = Depends(get_db)):
    """
    Records customer opt-out from communications. Immediately stops recovery and closes the case.
    """
    case = db.query(models.RecoveryCase).filter(models.RecoveryCase.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

    case.opted_out = True
    case.status = CaseStatus.CLOSED

    audit_log = models.AuditLog(
        case_id=case_id,
        action_type="CUSTOMER_OPT_OUT",
        description="Customer opted out of recovery communications",
        reasoning=body.reason or "Customer requested communication suppression. Case closed."
    )
    db.add(audit_log)
    db.commit()

    return {"status": "opted_out", "case_id": case_id, "case_status": case.status.value}

