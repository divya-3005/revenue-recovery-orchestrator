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
  GET  /api/v1/analytics          — Recovery analytics dashboard
  GET  /api/v1/policy             — Current policy configuration
  POST /api/v1/demo/seed          — Seed 5 demo scenarios
  POST /api/v1/demo/confirm-payment/{id} — Simulate payment
  GET  /health                    — Health check
"""

import json
import logging
from datetime import date
from typing import List

from fastapi import FastAPI, BackgroundTasks, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.database import Base, engine, get_db
from app.models import RecoveryCase, AuditLog, CaseType, CaseStatus, ApprovalStatus, DecisionResult
from app.schemas import (
    CaseCreateRequest, CaseResponse, AuditLogResponse, PolicyConfigResponse,
    PromiseToPayRequest, ApprovalRequest, OptOutRequest,
)
from app.policy import POLICY
from app.pipeline import run_pipeline, _audit
from app.synthetic import generate_batch, generate_demo_scenarios, compute_priority_score
from app.executor import Executor

# Create all tables on startup (no Alembic needed for a hackathon project)
Base.metadata.create_all(bind=engine)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Revenue Recovery Orchestrator", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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

    # Audit: signal received + normalized
    _audit(db, case.id, "SIGNAL_RECEIVED",
           f"Case created: {body.case_type.value}", "New recovery case ingested")
    db.commit()

    # Run the recovery pipeline in the background
    background_tasks.add_task(_run_pipeline_safe, case.id)

    logger.info(f"Created case {case.id} for {case.customer_id}")
    return case


# ── Webhook Ingestion (Feature 1 — real signal listeners) ───────────────

@app.post("/api/v1/webhooks/razorpay", status_code=status.HTTP_200_OK)
def razorpay_webhook(
    event_payload: dict,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Webhook listener for Razorpay events.
    Handles payment failures → new cases, and payment success → recovery confirmation.
    """
    event_name = event_payload.get("event", "")
    data = event_payload.get("payload", {})

    # ── Payment success → close the linked case ──
    if event_name in ("payment.captured", "order.paid", "invoice.paid", "payment_link.paid"):
        payment_entity = data.get("payment", {}).get("entity", {}) or data.get("payment_link", {}).get("entity", {})
        notes = payment_entity.get("notes", {})
        case_id = notes.get("case_id")
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
    """Reject an escalated action — re-runs the AI pipeline."""
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    if not case:
        raise HTTPException(404, "Case not found")
    if case.status != CaseStatus.AWAITING_APPROVAL:
        raise HTTPException(409, "Case is not awaiting approval")

    case.approval_status = ApprovalStatus.REJECTED
    case.status = CaseStatus.IN_PROGRESS
    _audit(db, case.id, "REJECTED", f"Rejected by {body.reviewer_id}",
           "Returned to AI pipeline for re-evaluation.")
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
def run_batch(simulate: bool = True, db: Session = Depends(get_db)):
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
            results.append(result)
        except Exception as e:
            logger.error(f"Batch pipeline error for {cid}: {e}")
            results.append({"status": "error", "case_id": cid, "reason": str(e)})

    return {"cases_created": len(created_ids), "case_ids": created_ids}


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
                "breakdown_by_status": {}, "breakdown_by_channel": {}, "exceptions": []}

    at_risk = sum(c.amount_paise for c in cases)
    recovered_count = sum(1 for c in cases if c.status == CaseStatus.RECOVERED)
    recovered_paise = sum(c.recovered_amount_paise for c in cases)
    discounts = sum(c.cumulative_discount_paise for c in cases)
    comms_cost = sum(c.cumulative_comms_cost_paise for c in cases)
    net = recovered_paise - discounts - comms_cost

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

    # Exception list (Feature 12)
    exceptions = [
        {"case_id": c.id, "status": c.status.value, "case_type": c.case_type.value,
         "amount_paise": c.amount_paise}
        for c in cases if c.status in (CaseStatus.FAILED, CaseStatus.ESCALATED, CaseStatus.CLOSED)
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
    """Execute a human-approved action."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
        if not case or not case.pending_decision_json:
            return
        if case.approval_status != ApprovalStatus.APPROVED:
            return

        decision = DecisionResult.model_validate(case.pending_decision_json)
        executor = Executor()
        exec_result = executor.execute(case, decision)

        _audit(db, case.id, "ACTION_EXECUTED",
               f"Approved action executed: {exec_result.status.value}",
               json.dumps({"reason": exec_result.reason,
                           "external_ref": exec_result.external_reference_id}))

        from app.models import ExecutionStatus
        if exec_result.status in (ExecutionStatus.SUCCESS, ExecutionStatus.DRY_RUN):
            case.status = CaseStatus.PAYMENT_PENDING
        else:
            case.status = CaseStatus.FAILED

        db.commit()
    except Exception as e:
        logger.error(f"Approved execution error for {case_id}: {e}")
    finally:
        db.close()


def _simulate_payment(db: Session, case_id: str):
    """Simulate a customer completing payment (for demo/batch)."""
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    if not case:
        return
    if case.status not in (CaseStatus.PAYMENT_PENDING, CaseStatus.IN_PROGRESS, CaseStatus.OPEN):
        return

    case.recovered_amount_paise = case.amount_paise
    case.status = CaseStatus.RECOVERED
    _audit(db, case.id, "PAYMENT_CONFIRMED",
           f"Payment confirmed for {case.amount_paise} paise (simulated)",
           "Simulated payment for demo/batch processing")
    db.commit()
