"""
Revenue Recovery Orchestrator — FastAPI Application.

One orchestrator for three revenue-at-risk types:
  - Failed subscription/mandate charges
  - Abandoned checkouts
  - Overdue invoices/payment links

Endpoints:
  POST /api/v1/cases              — Ingest a new recovery case
  GET  /api/v1/cases              — List all cases (sorted by priority)
  GET  /api/v1/cases/{id}/audit   — Full audit trail for one case
  GET  /api/v1/cases/escalated    — Cases needing human review
  POST /api/v1/cases/{id}/approve — Human approval gate
  POST /api/v1/cases/{id}/reject  — Reject an escalated action
  POST /api/v1/cases/{id}/close   — Close an escalated case
  POST /api/v1/cases/{id}/promise-to-pay — Capture promise-to-pay date
  POST /api/v1/cases/{id}/opt-out — Customer opt-out
  POST /api/v1/batch              — Generate & process 50+ synthetic cases
  POST /api/v1/jobs/run-follow-ups — Re-engage stale unpaid cases
  GET  /api/v1/analytics          — Recovery analytics dashboard
  GET  /api/v1/policy             — Current policy configuration
  POST /api/v1/demo/seed          — Seed 5 demo scenarios
  POST /api/v1/demo/confirm-payment/{id} — Simulate payment
  GET  /health                    — Health check
"""

import asyncio
import json
import logging
import hmac
import hashlib
import os
from datetime import date
from typing import List

from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, Depends, HTTPException, status, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.database import Base, engine, get_db
from app.models import (
    RecoveryCase, AuditLog, CaseType, CaseStatus, ApprovalStatus,
    DecisionResult, DiagnosisResult, ExecutionStatus, RecoveryActionType,
)
from app.schemas import (
    CaseCreateRequest, CaseResponse, AuditLogResponse, PolicyConfigResponse,
    PromiseToPayRequest, ApprovalRequest, OptOutRequest,
)
from app.policy import POLICY, evaluate_policy
from app.pipeline import run_pipeline, run_follow_up_check, _audit, CUSTOMER_FACING_ACTIONS
from app.synthetic import generate_batch, generate_demo_scenarios, compute_priority_score
from app.executor import Executor
from app.comms import generate_message

# Create all tables on startup (no Alembic needed for a hackathon project)
Base.metadata.create_all(bind=engine)

# No Alembic in scope — add any columns missing from an older SQLite file
# so a stale recovery.db doesn't 500 on the first insert.
if engine.url.get_backend_name() == "sqlite":
    from sqlalchemy import inspect, text
    existing = {c["name"] for c in inspect(engine).get_columns("recovery_cases")}
    for col, ddl in [("contact_count", "INTEGER NOT NULL DEFAULT 0"),
                     ("follow_up_count", "INTEGER NOT NULL DEFAULT 0"),
                     ("last_rejection_note", "VARCHAR"),
                     ("scheduled_for", "DATETIME")]:
        if col not in existing:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE recovery_cases ADD COLUMN {col} {ddl}"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The scheduler is disabled during pytest runs because background async loops 
    # conflict with pytest-asyncio's strict event loop lifecycle management.
    if os.getenv("ENABLE_FOLLOW_UP_SCHEDULER") == "1" and not os.getenv("PYTEST_CURRENT_TEST"):
        task = asyncio.create_task(_follow_up_scheduler_loop())
        logger.info("Follow-up scheduler started.")
        yield
        task.cancel()
    else:
        yield


app = FastAPI(title="Revenue Recovery Orchestrator", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Optional background scheduler for the follow-up re-loop ─────────────

async def _follow_up_scheduler_loop():
    from app.database import SessionLocal
    interval = int(os.getenv("FOLLOW_UP_POLL_SECONDS", "3600"))
    while True:
        await asyncio.sleep(interval)
        db = SessionLocal()
        try:
            results = run_follow_up_check(db)
            if results:
                logger.info(f"Follow-up scheduler: re-engaged {len(results)} case(s).")
        except Exception as e:
            logger.error(f"Follow-up scheduler error: {e}")
        finally:
            db.close()


# ── Health ───────────────────────────────────────────────────────────────

@app.get("/health")
def health_check():
    return {"status": "healthy"}


# ── Case Ingestion (Feature 1) ──────────────────────────────────────────

@app.post("/api/v1/cases", response_model=CaseResponse, status_code=status.HTTP_201_CREATED)
def create_case(
    body: CaseCreateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Ingest a new risk signal and normalize it into a Recovery Case."""
    priority = compute_priority_score(body.case_type.value, body.amount_paise, body.raw_signal_payload)

    case = RecoveryCase(
        case_type=body.case_type,
        amount_paise=body.amount_paise,
        currency=body.currency,
        customer_id=body.customer_id,
        customer_email=body.customer_email or body.raw_signal_payload.get("email", f"{body.customer_id}@example.com"),
        customer_phone=body.customer_phone or body.raw_signal_payload.get("contact"),
        payment_rail=body.payment_rail,
        priority_score=priority,
        raw_signal_payload=body.raw_signal_payload,
    )
    db.add(case)
    db.commit()
    db.refresh(case)

    _audit(db, case.id, "SIGNAL_RECEIVED",
           f"Case created: {body.case_type.value}", "New recovery case ingested")
    db.commit()

    background_tasks.add_task(_run_pipeline_safe, case.id)

    logger.info(f"Created case {case.id} for {case.customer_id}")
    return case


# ── Webhook Ingestion (Feature 1 — real signal listeners) ───────────────

@app.post("/api/v1/webhooks/razorpay", status_code=status.HTTP_200_OK)
async def razorpay_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Webhook listener for Razorpay events.
    Verifies HMAC signature, enforces idempotency, and routes events.
    """
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET")

    if secret:
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(400, "Invalid webhook signature")

    try:
        event_payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON body")

    event_id = request.headers.get("x-razorpay-event-id") or event_payload.get("id")
    if not event_id:
        return {"status": "ignored", "reason": "no event id"}

    # Idempotency check: Have we processed this event before?
    existing = db.query(RecoveryCase).filter(RecoveryCase.razorpay_event_id == event_id).first()
    if existing:
        return {"status": "ignored", "reason": "duplicate event"}

    event_name = event_payload.get("event", "")
    data = event_payload.get("payload", {})

    # ── Payment success → close the linked case ──
    if event_name in ("payment.captured", "order.paid", "invoice.paid", "payment_link.paid"):
        case_id = None
        for key in ("payment_link", "payment", "invoice", "order"):
            notes = (data.get(key, {}).get("entity", {}) or {}).get("notes", {}) or {}
            case_id = notes.get("case_id")
            if case_id:
                break
        if case_id:
            _simulate_payment(db, case_id)
            return {"status": "payment_confirmed", "case_id": case_id}
        return {"status": "ignored", "reason": "no case_id in notes"}

    # ── Payment failure / subscription failure → create case ──
    case_type = None
    amount = 0
    customer_id = "unknown"
    payment_rail = None
    raw_payload = event_payload

    if "subscription" in event_name or event_name == "payment.failed":
        case_type = CaseType.SUBSCRIPTION_FAILED
        pe = data.get("payment", {}).get("entity", {})
        amount = pe.get("amount", 0)
        customer_id = pe.get("customer_id") or pe.get("email") or "cust_webhook"
        payment_rail = pe.get("method", "card")
        raw_payload = {
            "reason": pe.get("error_code") or pe.get("error_reason") or "payment_failed",
            "email": pe.get("email"),
            "contact": pe.get("contact"),
            "error_description": pe.get("error_description"),
        }
    elif "invoice" in event_name:
        case_type = CaseType.INVOICE_OVERDUE
        ie = data.get("invoice", {}).get("entity", {})
        amount = ie.get("amount", 0)
        customer_id = ie.get("customer_id") or ie.get("customer_email") or "cust_invoice"
        raw_payload = {
            "invoice_number": ie.get("invoice_number", "INV-WH"),
            "days_overdue": 1,
            "email": ie.get("customer_email"),
            "contact": ie.get("customer_phone"),
        }

    if not case_type or amount <= 0:
        return {"status": "ignored", "event": event_name}

    priority = compute_priority_score(case_type.value, amount, raw_payload)
    case = RecoveryCase(
        case_type=case_type, amount_paise=amount, customer_id=customer_id,
        customer_email=raw_payload.get("email"), customer_phone=raw_payload.get("contact"),
        payment_rail=payment_rail, priority_score=priority, raw_signal_payload=raw_payload,
        razorpay_event_id=event_id,
    )
    db.add(case)
    db.commit()
    db.refresh(case)
    _audit(db, case.id, "SIGNAL_RECEIVED", f"Webhook: {event_name}", f"Razorpay webhook: {event_name}")
    db.commit()
    background_tasks.add_task(_run_pipeline_safe, case.id)
    return {"status": "created", "case_id": case.id}


@app.post("/api/v1/beacon/checkout-abandoned", status_code=status.HTTP_201_CREATED)
def checkout_abandoned_beacon(
    payload: dict,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Beacon endpoint for abandoned checkout signals from the frontend."""
    amount = payload.get("amount_paise", 0)
    customer_id = payload.get("customer_id", "cust_checkout")
    priority = compute_priority_score(CaseType.CHECKOUT_ABANDONED.value, amount, payload)
    case = RecoveryCase(
        case_type=CaseType.CHECKOUT_ABANDONED, amount_paise=amount, customer_id=customer_id,
        customer_email=payload.get("email"), customer_phone=payload.get("contact"),
        payment_rail=payload.get("payment_rail", "upi"), priority_score=priority,
        raw_signal_payload=payload,
    )
    db.add(case)
    db.commit()
    db.refresh(case)
    _audit(db, case.id, "SIGNAL_RECEIVED", "Checkout drop-off beacon", "Frontend abandonment beacon")
    db.commit()
    background_tasks.add_task(_run_pipeline_safe, case.id)
    return {"status": "created", "case_id": case.id}


# ── Case Listing ─────────────────────────────────────────────────────────

@app.get("/api/v1/cases", response_model=List[CaseResponse])
def list_cases(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    """List all cases, sorted by priority score (Feature 13: Recovery Prioritization)."""
    return db.query(RecoveryCase).order_by(
        RecoveryCase.priority_score.desc(),
        RecoveryCase.created_at.desc(),
    ).offset(skip).limit(limit).all()


# ── Audit Trail (Feature 10) ────────────────────────────────────────────

@app.get("/api/v1/cases/{case_id}/audit", response_model=List[AuditLogResponse])
def get_audit_trail(case_id: str, db: Session = Depends(get_db)):
    """Full audit trail: signal → diagnosis → decision → policy → execution."""
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    if not case:
        raise HTTPException(404, f"Case {case_id} not found")
    return db.query(AuditLog).filter(AuditLog.case_id == case_id).order_by(AuditLog.created_at).all()


# ── Escalation Queue (Feature 9) ────────────────────────────────────────

@app.get("/api/v1/cases/escalated", response_model=List[CaseResponse])
def list_escalated(db: Session = Depends(get_db)):
    """Cases needing human review — full context attached."""
    return db.query(RecoveryCase).filter(
        RecoveryCase.status.in_([CaseStatus.ESCALATED, CaseStatus.AWAITING_APPROVAL])
    ).order_by(RecoveryCase.priority_score.desc()).all()


# ── Human Approval Gate (Feature 15) ────────────────────────────────────

@app.post("/api/v1/cases/{case_id}/approve")
def approve_case(
    case_id: str,
    body: ApprovalRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Approve an escalated action — triggers execution."""
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    if not case:
        raise HTTPException(404, "Case not found")
    if case.status != CaseStatus.AWAITING_APPROVAL or case.approval_status != ApprovalStatus.PENDING:
        raise HTTPException(409, "Case is not awaiting approval")
    if case.pending_decision_id != body.decision_id or case.pending_decision_hash != body.decision_hash:
        raise HTTPException(409, "Decision ID/hash mismatch — stale approval")

    case.approval_status = ApprovalStatus.APPROVED
    case.approved_decision_id = body.decision_id
    case.approved_decision_hash = body.decision_hash
    case.last_rejection_note = None
    _audit(db, case.id, "APPROVED", f"Approved by {body.reviewer_id}",
           f"Decision {body.decision_id} approved for execution.")
    db.commit()

    # Execute the approved action in background
    background_tasks.add_task(_execute_approved, case_id)
    return {"status": "approved", "case_id": case_id}


@app.post("/api/v1/cases/{case_id}/reject")
def reject_case(
    case_id: str,
    body: ApprovalRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Reject an escalated action — stores feedback, stops automating this case, and re-runs the AI pipeline."""
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    if not case:
        raise HTTPException(404, "Case not found")
    if case.status != CaseStatus.AWAITING_APPROVAL:
        raise HTTPException(409, "Case is not awaiting approval")

    case.approval_status = ApprovalStatus.REJECTED
    case.status = CaseStatus.IN_PROGRESS
    # Store rejection feedback so the AI doesn't re-propose the same action
    case.last_rejection_note = getattr(body, 'note', None) or "Reviewer rejected the proposed action."
    case.pending_decision_json = None
    case.pending_decision_id = None
    case.pending_decision_hash = None
    _audit(db, case.id, "REJECTED", f"Rejected by {body.reviewer_id}",
           f"Feedback: {case.last_rejection_note}")
    db.commit()

    background_tasks.add_task(_run_pipeline_safe, case_id)
    return {"status": "rejected", "case_id": case_id}


@app.post("/api/v1/cases/{case_id}/close")
def close_case(case_id: str, db: Session = Depends(get_db)):
    """Manually close an escalated/approval-pending case."""
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    if not case:
        raise HTTPException(404, "Case not found")
    if case.status not in (CaseStatus.ESCALATED, CaseStatus.AWAITING_APPROVAL):
        raise HTTPException(400, "Can only close escalated or approval-pending cases")

    case.status = CaseStatus.CLOSED
    case.last_rejection_note = None
    _audit(db, case.id, "MANUAL_CLOSE", "Case closed by human reviewer.", "")
    db.commit()
    return {"status": "closed", "case_id": case_id}


# ── Promise-to-Pay (Feature 14) ─────────────────────────────────────────

@app.post("/api/v1/cases/{case_id}/promise-to-pay")
def capture_promise_to_pay(case_id: str, body: PromiseToPayRequest, db: Session = Depends(get_db)):
    """Record a customer's commitment to pay by a specific date."""
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    if not case:
        raise HTTPException(404, "Case not found")
    if case.status in (CaseStatus.RECOVERED, CaseStatus.CLOSED):
        raise HTTPException(400, f"Case already resolved ({case.status.value})")
    if body.date < date.today():
        raise HTTPException(400, "Promise date must be today or in the future")

    case.promise_to_pay_date = body.date
    _audit(db, case.id, "PTP_CAPTURED",
           f"Promise-to-pay: customer committed to pay by {body.date}",
           f"{body.note or 'No note provided.'} Reminders suppressed until {body.date}.")
    db.commit()
    return {"status": "captured", "case_id": case_id, "promise_to_pay_date": str(body.date)}


# ── Customer Opt-Out (Feature 8) ────────────────────────────────────────

@app.post("/api/v1/cases/{case_id}/opt-out")
def opt_out(case_id: str, body: OptOutRequest = OptOutRequest(), db: Session = Depends(get_db)):
    """Customer opt-out — immediately stops recovery and closes the case."""
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    if not case:
        raise HTTPException(404, "Case not found")

    case.opted_out = True
    case.status = CaseStatus.CLOSED
    _audit(db, case.id, "OPT_OUT", "Customer opted out of recovery.", body.reason or "")
    db.commit()
    return {"status": "opted_out", "case_id": case_id}


# ── Batch Processing (Feature 11) ───────────────────────────────────────

@app.post("/api/v1/batch")
def run_batch(db: Session = Depends(get_db)):
    """Generate 50+ synthetic cases and process them through the full pipeline."""
    synthetic = generate_batch()
    created_ids = []

    for data in synthetic:
        case = RecoveryCase(
            case_type=data["case_type"],
            amount_paise=data["amount_paise"],
            currency=data["currency"],
            customer_id=data["customer_id"],
            customer_email=data.get("customer_email"),
            customer_phone=data.get("customer_phone"),
            payment_rail=data.get("payment_rail"),
            priority_score=data.get("priority_score", 0),
            raw_signal_payload=data["raw_signal_payload"],
        )
        db.add(case)
        db.commit()
        db.refresh(case)
        _audit(db, case.id, "SIGNAL_RECEIVED",
               f"Batch case: {data['case_type']}", "Synthetic batch case")
        db.commit()
        created_ids.append(case.id)

    # Run the pipeline for each case
    results = []
    for idx, cid in enumerate(created_ids):
        try:
            result = run_pipeline(db, cid)
            # Simulate payment for ~60% of payment_pending cases
            if result.get("status") == "payment_pending" and idx % 5 < 3:
                _simulate_payment(db, cid)
                result = {"status": "recovered", "reason": "Simulated payment received."}
            results.append({"case_id": cid, **result})
        except Exception as e:
            logger.error(f"Batch pipeline error for {cid}: {e}")
            results.append({"case_id": cid, "status": "error", "reason": str(e)})

    status_counts: dict = {}
    for r in results:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

    return {
        "cases_created": len(created_ids),
        "case_ids": created_ids,
        "results": results,
        "status_counts": status_counts,
        "outcomes_simulated": True,
    }


# ── Follow-Up Re-loop (Feature 6 tone ramp + Feature 7 re-loop) ─────────

@app.post("/api/v1/jobs/run-follow-ups")
def run_follow_ups(force: bool = False, db: Session = Depends(get_db)):
    """
    Re-engage PAYMENT_PENDING cases with an escalated tone/channel.
    force=True bypasses the follow_up_after_hours window — use this in demos.
    """
    results = run_follow_up_check(db, force=force)
    status_counts: dict = {}
    for r in results:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
    return {
        "cases_checked": len(results),
        "forced": force,
        "results": results,
        "status_counts": status_counts,
    }


# ── Demo Endpoints ──────────────────────────────────────────────────────

@app.post("/api/v1/demo/seed")
def seed_demo(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Seed 5 canonical demo scenarios."""
    scenarios = generate_demo_scenarios()
    created_ids = []

    for data in scenarios:
        case = RecoveryCase(
            case_type=data["case_type"],
            amount_paise=data["amount_paise"],
            currency=data["currency"],
            customer_id=data["customer_id"],
            customer_email=data.get("customer_email"),
            customer_phone=data.get("customer_phone"),
            payment_rail=data.get("payment_rail"),
            priority_score=data.get("priority_score", 0),
            raw_signal_payload=data["raw_signal_payload"],
        )
        db.add(case)
        db.commit()
        db.refresh(case)
        _audit(db, case.id, "SIGNAL_RECEIVED", f"Demo: {data['case_type']}", "Demo scenario")
        db.commit()
        created_ids.append(case.id)

    # Process each demo case in background
    for cid in created_ids:
        background_tasks.add_task(_run_pipeline_safe, cid)

    return {"status": "seeded", "count": len(created_ids), "case_ids": created_ids}


@app.post("/api/v1/demo/confirm-payment/{case_id}")
def demo_confirm_payment(case_id: str, db: Session = Depends(get_db)):
    """Demo-only: simulate a customer payment."""
    _simulate_payment(db, case_id)
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    return {"status": case.status.value if case else "not_found", "case_id": case_id}


# ── Recovery Analytics (Feature 12 + 17) ────────────────────────────────

@app.get("/api/v1/analytics")
def get_analytics(db: Session = Depends(get_db)):
    """Aggregated recovery metrics including net economics (Feature 17)."""
    cases = db.query(RecoveryCase).all()
    total = len(cases)
    if total == 0:
        return {"total_cases": 0, "total_at_risk_paise": 0, "total_recovered_paise": 0,
                "net_recovered_paise": 0, "recovery_rate_percent": 0.0,
                "net_recovery_rate_percent": 0.0, "total_discount_cost_paise": 0,
                "total_comms_cost_paise": 0, "breakdown_by_case_type": {},
                "breakdown_by_status": {}, "breakdown_by_channel": {},
                "exceptions": [], "stopped_by_rule": []}

    at_risk = sum(c.amount_paise for c in cases)
    recovered_count = sum(1 for c in cases if c.status == CaseStatus.RECOVERED)
    recovered_paise = sum(c.recovered_amount_paise for c in cases)
    discounts = sum(c.cumulative_discount_paise for c in cases)
    comms_cost = sum(c.cumulative_comms_cost_paise for c in cases)
    net = recovered_paise - comms_cost

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
    for c in cases:
        ch = c.latest_channel or c.payment_rail or "email"
        if ch not in by_channel:
            by_channel[ch] = {"total": 0, "recovered": 0, "at_risk_paise": 0, "recovered_paise": 0}
        by_channel[ch]["total"] += 1
        by_channel[ch]["at_risk_paise"] += c.amount_paise
        if c.status == CaseStatus.RECOVERED:
            by_channel[ch]["recovered"] += 1
        by_channel[ch]["recovered_paise"] += c.recovered_amount_paise

    # Split exceptions (unresolved) from stopped-by-rule (system working correctly)
    UNRESOLVED = (CaseStatus.FAILED, CaseStatus.ESCALATED)
    exceptions = [
        {"case_id": c.id, "status": c.status.value, "case_type": c.case_type.value,
         "amount_paise": c.amount_paise}
        for c in cases if c.status in UNRESOLVED
    ]
    stopped_by_rule = [
        {"case_id": c.id, "status": c.status.value, "case_type": c.case_type.value,
         "amount_paise": c.amount_paise}
        for c in cases if c.status == CaseStatus.CLOSED
    ]

    return {
        "total_cases": total,
        "total_at_risk_paise": at_risk,
        "total_recovered_paise": recovered_paise,
        "total_discount_cost_paise": discounts,
        "total_comms_cost_paise": comms_cost,
        "net_recovered_paise": net,
        "recovery_rate_percent": round(recovered_count / total * 100, 2),
        "net_recovery_rate_percent": round(net / at_risk * 100, 2) if at_risk > 0 else 0.0,
        "breakdown_by_case_type": by_type,
        "breakdown_by_status": by_status,
        "breakdown_by_channel": by_channel,
        "exceptions": exceptions,
        "stopped_by_rule": stopped_by_rule,
    }


# ── Policy Config (Feature 3: visible, inspectable) ─────────────────────

@app.get("/api/v1/policy", response_model=PolicyConfigResponse)
def get_policy():
    return PolicyConfigResponse(
        max_retries=POLICY["max_retries"],
        max_discount_percent=POLICY["max_discount_percent"],
        require_human_approval_above_paise=POLICY["require_human_approval_above_paise"],
        block_hard_declines=POLICY["block_hard_declines"],
        min_confidence_score=POLICY["min_confidence_score"],
        pre_debit_notice_hours=POLICY["pre_debit_notice_hours"],
        max_days_pursued=POLICY["max_days_pursued"],
        follow_up_after_hours=POLICY["follow_up_after_hours"],
        max_follow_ups=POLICY["max_follow_ups"],
    )


# ── Background helpers ──────────────────────────────────────────────────

def _run_pipeline_safe(case_id: str):
    """Run the pipeline in a background task with its own DB session."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        run_pipeline(db, case_id)
    except Exception as e:
        logger.error(f"Pipeline error for {case_id}: {e}")
    finally:
        db.close()


def _execute_approved(case_id: str):
    """Execute a human-approved action, re-checking deterministic money
    caps a human is NOT allowed to override."""
    from app.database import SessionLocal

    db = SessionLocal()
    case = None
    try:
        case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
        if not case or not case.pending_decision_json:
            return
        if case.approval_status != ApprovalStatus.APPROVED:
            return

        decision = DecisionResult.model_validate(case.pending_decision_json)

        # Integrity: the stored decision must still hash to what was approved.
        if decision.canonical_hash() != case.approved_decision_hash:
            _audit(db, case.id, "APPROVAL_INTEGRITY_FAILED",
                   "Stored decision no longer matches the approved hash — refusing to execute.",
                   "Possible tampering or a stale write; returned to escalation queue.")
            case.status = CaseStatus.ESCALATED
            db.commit()
            return

        # Re-run the guardrails. Human approval clears the high-value gate
        # only — it cannot override the discount cap or the hard-decline block.
        diagnosis = DiagnosisResult.model_validate(case.pending_diagnosis_json)
        policy = evaluate_policy(case, decision, diagnosis, human_approved=True)
        _audit(db, case.id, "POLICY_EVALUATED",
               f"Post-approval re-check {'APPROVED' if policy.allowed else 'REJECTED'}: {policy.reason}",
               json.dumps({"human_approved": True, "rules": policy.rules_checked}))
        if not policy.allowed:
            case.status = CaseStatus.FAILED
            _audit(db, case.id, "POLICY_BLOCKED",
                   f"Approved action still breaches a hard cap: {policy.reason}",
                   "Human approval does not override discount/hard-decline limits.")
            db.commit()
            return

        action = decision.recommended_action
        if action in CUSTOMER_FACING_ACTIONS:
            case.contact_count += 1
            channel = decision.action_parameters.get("channel", "email")
            message = generate_message(case, diagnosis, case.contact_count, channel)
            case.latest_comms_preview = message
            case.latest_channel = channel
            case.cumulative_comms_cost_paise += 25
            _audit(db, case.id, "COMMUNICATION_GENERATED",
                   f"Message generated (contact #{case.contact_count}, channel: {channel})", message)

        exec_result = Executor().execute(case, decision)
        _audit(db, case.id, "ACTION_EXECUTED",
               f"Approved action executed: {exec_result.status.value}",
               json.dumps({"reason": exec_result.reason,
                           "external_ref": exec_result.external_reference_id,
                           "params": exec_result.action_parameters_used}))

        if exec_result.status in (ExecutionStatus.SUCCESS, ExecutionStatus.DRY_RUN):
            if exec_result.action_taken == RecoveryActionType.OFFER_DISCOUNT:
                case.cumulative_discount_paise += exec_result.action_parameters_used.get(
                    "discount_applied_paise", 0)
            case.status = CaseStatus.PAYMENT_PENDING
            _audit(db, case.id, "AWAITING_PAYMENT",
                   f"Approved action complete — awaiting payment. "
                   f"Next follow-up in {POLICY['follow_up_after_hours']}h.", exec_result.reason)
        else:
            case.status = CaseStatus.ESCALATED
            _audit(db, case.id, "EXECUTION_FAILED",
                   f"Approved action failed to execute: {exec_result.reason}",
                   "Returned to the escalation queue for manual handling.")
        db.commit()

    except Exception as e:
        logger.error(f"Approved execution error for {case_id}: {e}")
        db.rollback()
        try:
            case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
            if case:
                case.status = CaseStatus.ESCALATED
                _audit(db, case.id, "EXECUTION_ERROR",
                       f"Approved action could not be executed: {e}",
                       "Case returned to the escalation queue.")
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


def _simulate_payment(db: Session, case_id: str):
    """Simulate a customer completing payment (for demo/batch)."""
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    if not case:
        return
    if case.status not in (CaseStatus.PAYMENT_PENDING, CaseStatus.IN_PROGRESS, CaseStatus.OPEN):
        return

    actual_paid = max(0, case.amount_paise - case.cumulative_discount_paise)
    case.recovered_amount_paise = actual_paid
    case.status = CaseStatus.RECOVERED
    _audit(db, case.id, "PAYMENT_CONFIRMED",
           f"Payment confirmed for {actual_paid} paise",
           "Payment confirmed and case marked as recovered")
    db.commit()
