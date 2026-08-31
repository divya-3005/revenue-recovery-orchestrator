"""
Regression tests for the follow-up audit findings:

1. Feature 6/7's tone/channel escalation ramp never actually re-fired —
   nothing re-checked a PAYMENT_PENDING case that never got paid, so the
   system behaved like a one-shot script despite the spec's framing.
   Fixed by pipeline.run_follow_up_check() + POST /api/v1/jobs/run-follow-ups.

2. Communications were generated and billed even for actions that never
   contact the customer (escalate_to_human, stop). Fixed by gating Step 6
   behind pipeline.CUSTOMER_FACING_ACTIONS.

3. The out-of-the-box (no GEMINI_API_KEY) rule-based decision engine only
   ever produced "email" as a channel and never selected retry_charge,
   switch_rail, or send_reminder — three of the seven documented actions
   were dead code. Fixed via ai._fallback_decide's staged
   retry_charge -> switch_rail -> create_payment_link logic and
   ai._pick_channel.

4. The RBI eNACH/NACH pre-debit delay was only computed for SOFT_DECLINE,
   not MISSED_PAYMENT — an overdue invoice on an eNACH rail could get
   hard-blocked to FAILED by policy instead of getting a compliant delay.
   Fixed in ai._fallback_decide's MISSED_PAYMENT branch.
"""
from datetime import datetime, timedelta, timezone

from tests.test_pipeline import SessionLocal, client
from app.models import CaseType, CaseStatus, RecoveryCase, AuditLog
from app.pipeline import run_pipeline, run_follow_up_check
from app.ai import _fallback_decide
from app.models import DiagnosisResult, RootCauseCategory
from app.policy import evaluate_policy, POLICY


def _create_case_direct(case_type, amount, payload=None, payment_rail=None):
    db = SessionLocal()
    try:
        case = RecoveryCase(
            case_type=CaseType(case_type),
            amount_paise=amount,
            customer_id="cust_direct",
            payment_rail=payment_rail,
            raw_signal_payload=payload or {},
        )
        db.add(case)
        db.commit()
        db.refresh(case)
        return case.id
    finally:
        db.close()


def _age_case(case_id, hours):
    """Force a case's updated_at and scheduled_for into the past, simulating real elapsed
    time without needing the test to actually sleep."""
    db = SessionLocal()
    try:
        case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
        case.updated_at = datetime.now(timezone.utc) - timedelta(hours=hours)
        if getattr(case, "scheduled_for", None):
            case.scheduled_for = case.scheduled_for - timedelta(hours=hours)
        db.commit()
    finally:
        db.close()


def _audit_types(case_id):
    db = SessionLocal()
    try:
        return [
            l.action_type for l in
            db.query(AuditLog).filter(AuditLog.case_id == case_id)
            .order_by(AuditLog.created_at).all()
        ]
    finally:
        db.close()


def test_soft_decline_on_mandate_rail_skips_silent_retry():
    """A fresh soft decline on an eNACH mandate rail must NOT get
    retry_charge — RBI pre-debit notice applies to any debit attempt on a
    mandate, not just a customer-facing link, so an immediate silent retry
    isn't compliant. It should go straight to a delayed, compliant
    create_payment_link instead. (Regression: making retry_charge reachable
    exposed this — 5 enach-rail soft declines in the 50-case batch started
    landing in FAILED via policy Rule 8 rejecting a same-day silent retry.)"""
    case_id = _create_case_direct(
        "subscription_failed", 99_900,
        payload={"reason": "insufficient_funds"}, payment_rail="enach",
    )
    run_pipeline(SessionLocal(), case_id)

    db = SessionLocal()
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    assert case.latest_action_recommended == "create_payment_link"
    assert case.status != CaseStatus.FAILED, (
        "eNACH-rail soft decline was blocked to FAILED instead of "
        "skipping straight to a compliant delayed payment link."
    )
    assert case.status == CaseStatus.PAYMENT_PENDING
    db.close()


# ── Fix 1 + 3: the re-loop actually re-fires, and stages through the
#    previously-dead retry_charge -> switch_rail -> create_payment_link
#    actions with an email -> sms channel switch. ─────────────────────────

def test_follow_up_reengages_stale_payment_pending_case():
    """A fresh subscription soft decline should be retried silently first
    (retry_charge, no customer contact). Once it's gone stale in
    PAYMENT_PENDING, the follow-up job should re-engage it — escalating
    to switch_rail on the first follow-up, and to create_payment_link on
    the second — switching the channel from email to sms along the way."""
    case_id = _create_case_direct(
        "subscription_failed", 50_000,
        payload={"reason": "insufficient_funds"}, payment_rail="card",
    )

    # Pass 0: fresh case, first response should be a silent retry_charge —
    # no customer contact, so no comms generated.
    run_pipeline(SessionLocal(), case_id)
    db = SessionLocal()
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    assert case.status == CaseStatus.PAYMENT_PENDING
    assert case.latest_action_recommended == "retry_charge"
    assert case.follow_up_count == 0
    assert "COMMUNICATION_GENERATED" not in _audit_types(case_id)
    db.close()

    # Simulate 60 real hours passing with no payment (default
    # follow_up_after_hours is 48) and run the follow-up job.
    _age_case(case_id, hours=60)
    results = run_follow_up_check(SessionLocal())
    assert any(r["case_id"] == case_id for r in results), (
        "Stale PAYMENT_PENDING case was not picked up by run_follow_up_check "
        "— the re-loop that's supposed to make this 'a system with memory, "
        "not a one-shot script' isn't actually re-checking anything."
    )

    db = SessionLocal()
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    assert case.follow_up_count == 1
    assert case.latest_action_recommended == "switch_rail", (
        "After one failed silent retry, the second pass should try a "
        "different rail before asking for a brand-new link on the one "
        "that just failed."
    )
    assert case.latest_channel == "email", (
        "First real customer contact (after a silent retry_charge) should be "
        "email — the gentle opener. SMS only fires on the second real contact."
    )
    assert case.contact_count == 1, (
        "A silent retry_charge must not consume a tone-ladder rung — the "
        "customer's first real message has to open at 'gentle'.")
    db.close()
    types_after_1 = _audit_types(case_id)
    assert types_after_1.count("COMMUNICATION_GENERATED") == 1
    assert any("contact #1" in l for l in
        [log.description for log in SessionLocal().query(AuditLog).filter(
            AuditLog.case_id == case_id, AuditLog.action_type == "COMMUNICATION_GENERATED").all()])

    # A second follow-up (still unpaid) should now fall through to a plain
    # payment link, since one retry + one rail switch have both been tried.
    _age_case(case_id, hours=60)
    run_follow_up_check(SessionLocal())
    db = SessionLocal()
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    assert case.follow_up_count == 2
    assert case.latest_action_recommended == "create_payment_link"
    db.close()

    # A third follow-up exceeds max_follow_ups (2) — should escalate
    # instead of contacting the customer a fourth time.
    _age_case(case_id, hours=60)
    run_follow_up_check(SessionLocal())
    db = SessionLocal()
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    assert case.status == CaseStatus.ESCALATED
    assert "FOLLOWUPS_EXHAUSTED" in _audit_types(case_id)
    db.close()


def test_follow_up_respects_stopping_rules():
    """A stale PAYMENT_PENDING case that has since opted out must not be
    re-contacted just because the follow-up job found it."""
    case_id = _create_case_direct(
        "checkout_abandoned", 20_000,
        payload={"cart_value": 20_000, "is_repeat_customer": False},
    )
    run_pipeline(SessionLocal(), case_id)  # -> send_reminder, payment_pending

    db = SessionLocal()
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    case.opted_out = True
    db.commit()
    db.close()

    _age_case(case_id, hours=60)
    results = run_follow_up_check(SessionLocal())
    matching = [r for r in results if r["case_id"] == case_id]
    assert matching and matching[0]["status"] == "closed"

    db = SessionLocal()
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    assert case.status == CaseStatus.CLOSED
    db.close()


# ── Fix 2: escalate_to_human / stop never generate or bill for comms ────

def test_hard_decline_generates_no_customer_comms():
    """A hard decline is escalated straight to a human — the customer is
    never contacted, so no message should be drafted or billed for it."""
    case_id = _create_case_direct(
        "subscription_failed", 49_900,
        payload={"reason": "stolen_card"},
    )
    run_pipeline(SessionLocal(), case_id)

    db = SessionLocal()
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    assert case.status == CaseStatus.ESCALATED
    assert case.cumulative_comms_cost_paise == 0, (
        "Case was billed for a customer message even though the decided "
        "action (escalate_to_human) never contacts the customer."
    )
    db.close()
    assert "COMMUNICATION_GENERATED" not in _audit_types(case_id)


# ── Fix 4 (RBI mandate delay for invoices, not just subscriptions) ──────

def test_missed_payment_on_enach_rail_gets_compliant_delay():
    """An overdue invoice on an eNACH mandate rail must get the same
    72h RBI pre-debit delay a subscription soft decline gets — previously
    only the SOFT_DECLINE branch computed this, so an eNACH invoice would
    get the generic 24h default and be hard-blocked by policy Rule 8
    instead of proceeding compliantly."""
    case_id = _create_case_direct(
        "invoice_overdue", 40_000,
        payload={"payment_history": {"on_time_ratio": 0.9}},
        payment_rail="enach",
    )
    db = SessionLocal()
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    diagnosis = DiagnosisResult(
        root_cause_category=RootCauseCategory.MISSED_PAYMENT,
        specific_reason="cash_flow_delay", confidence_score=0.85, reasoning="test",
    )
    decision = _fallback_decide(case, diagnosis)
    assert decision.action_parameters["delay_hours"] >= POLICY["pre_debit_notice_hours"], (
        "MISSED_PAYMENT decisions on an eNACH rail must respect the same "
        "RBI pre-debit delay as SOFT_DECLINE — got "
        f"{decision.action_parameters.get('delay_hours')}h."
    )

    policy_result = evaluate_policy(case, decision, diagnosis)
    assert policy_result.allowed, (
        f"Policy blocked a compliant eNACH invoice decision: {policy_result.reason}"
    )
    db.close()

    # And end to end: the full pipeline should NOT fail this case out.
    run_pipeline(SessionLocal(), case_id)
    db = SessionLocal()
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    assert case.status != CaseStatus.FAILED, (
        "eNACH invoice was blocked to FAILED instead of proceeding with a "
        "compliant pre-debit delay."
    )
    db.close()


# ── API surface for the fixes ────────────────────────────────────────────

def test_run_follow_ups_endpoint_and_batch_returns_results():
    """POST /api/v1/jobs/run-follow-ups exists and works, and POST
    /api/v1/batch no longer silently discards its per-case results."""
    resp = client.post("/api/v1/jobs/run-follow-ups")
    assert resp.status_code == 200
    body = resp.json()
    assert "cases_checked" in body and "results" in body

    batch_resp = client.post("/api/v1/batch")
    assert batch_resp.status_code == 200
    batch_body = batch_resp.json()
    assert batch_body["cases_created"] >= 50
    assert len(batch_body["results"]) == batch_body["cases_created"], (
        "run_batch computed per-case results and then discarded them "
        "instead of returning them to the caller."
    )
    assert "status_counts" in batch_body

    policy_resp = client.get("/api/v1/policy")
    policy_body = policy_resp.json()
    assert "follow_up_after_hours" in policy_body
    assert "max_follow_ups" in policy_body


def test_approved_high_value_case_actually_executes():
    """The approval gate must survive a decision stored by the pipeline
    itself — not a hand-written dict that happens to validate."""
    case_id = _create_case_direct(
        "subscription_failed", 7_500_000,
        payload={"reason": "insufficient_funds"},
        payment_rail="card",
    )
    # Pipeline writes pending_decision_json via model_dump
    run_pipeline(SessionLocal(), case_id)

    db = SessionLocal()
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    assert case.status == CaseStatus.AWAITING_APPROVAL, (
        f"High-value case should hit the approval gate, got {case.status}")
    did = case.pending_decision_id
    dh = case.pending_decision_hash
    db.close()

    resp = client.post(f"/api/v1/cases/{case_id}/approve", json={
        "decision_id": did, "decision_hash": dh, "reviewer_id": "admin"})
    assert resp.status_code == 200

    # Wait for background task (TestClient runs them synchronously)
    db = SessionLocal()
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    assert case.status != CaseStatus.AWAITING_APPROVAL, (
        "Approved case is still parked in AWAITING_APPROVAL — the background "
        "execution silently failed (Bug #1: canonical_json() omits confidence_score).")
    assert "ACTION_EXECUTED" in _audit_types(case_id)
    db.close()


def test_webhook_reads_event_id_from_header():
    """Razorpay sends x-razorpay-event-id as a header, not a body field.
    The endpoint must accept it from the header."""
    resp = client.post(
        "/api/v1/webhooks/razorpay",
        headers={"x-razorpay-event-id": "evt_hdr_test_001"},
        json={
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_hdr_001", "amount": 50000,
                        "customer_id": "cust_hdr", "email": "hdr@example.com",
                        "method": "card", "error_reason": "insufficient_funds",
                    }
                }
            }
        })
    assert resp.status_code == 200
    assert resp.json()["status"] == "created"

    # Idempotency: same header event ID should be ignored
    resp2 = client.post(
        "/api/v1/webhooks/razorpay",
        headers={"x-razorpay-event-id": "evt_hdr_test_001"},
        json={"event": "payment.failed", "payload": {
            "payment": {"entity": {"id": "pay_hdr_002", "amount": 50000,
                                   "customer_id": "cust_hdr2", "method": "card"}}}})
    assert resp2.json()["status"] == "ignored"
    assert resp2.json()["reason"] == "duplicate event"


def test_payment_link_paid_confirms_via_link_entity_notes():
    """payment_link.paid carries the case_id in payment_link.entity.notes,
    not payment.entity.notes. The webhook must search all entity types."""
    # Create a case to confirm
    case_id = _create_case_direct(
        "checkout_abandoned", 30_000,
        payload={"is_repeat_customer": True, "cart_value": 30_000},
        payment_rail="upi",
    )
    run_pipeline(SessionLocal(), case_id)

    resp = client.post(
        "/api/v1/webhooks/razorpay",
        headers={"x-razorpay-event-id": "evt_plink_paid_001"},
        json={
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {"entity": {"id": "plink_1", "notes": {"case_id": case_id}}},
                "payment": {"entity": {"id": "pay_plink_1", "amount": 30_000}},
            }
        })
    assert resp.status_code == 200
    assert resp.json()["status"] == "payment_confirmed"

    db = SessionLocal()
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    assert case.status == CaseStatus.RECOVERED
    db.close()


def test_retry_charge_delay_does_not_block_force_reengagement():
    """RETRY_CHARGE is silent and immediate — it must not pick up the
    24h delay meant for CREATE_PAYMENT_LINK, or force=True (the whole
    point of which is instant demo re-engagement) silently does nothing."""
    case_id = _create_case_direct("subscription_failed", 50_000,
        payload={"reason": "insufficient_funds"}, payment_rail="card")
    run_pipeline(SessionLocal(), case_id)

    db = SessionLocal()
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    assert case.latest_action_recommended == "retry_charge"
    assert case.scheduled_for is None, (
        "A silent retry_charge should never set scheduled_for — it has no "
        "delay concept, and a non-null value here freezes it out of "
        "force=True re-engagement for no reason.")
    db.close()

    # No _age_case call here — this is the real path the demo button uses.
    results = run_follow_up_check(SessionLocal(), force=True)
    assert any(r["case_id"] == case_id for r in results), (
        "force=True is supposed to re-engage every unpaid case immediately "
        "for demo purposes — a stray scheduled_for silently defeated it.")

def test_discount_action_escalates_channel_on_second_contact():
    """offer_discount is customer-facing, so it must carry a channel and
    follow the same email->sms escalation as every other contact action."""
    case_id = _create_case_direct("checkout_abandoned", 200_000,
        payload={"is_repeat_customer": True, "cart_value": 200_000}, payment_rail="upi")
    run_pipeline(SessionLocal(), case_id)

    db = SessionLocal()
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    assert case.latest_channel == "email"          # first contact
    case.scheduled_for = None
    db.commit(); db.close()

    run_follow_up_check(SessionLocal(), force=True)
    db = SessionLocal()
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    assert case.contact_count == 2
    assert case.latest_channel == "sms", (
        "Second contact must escalate to SMS — offer_discount was omitting "
        "the channel param, so it silently defaulted to email forever.")
    db.close()


def test_followups_exhausted_escalates_even_while_scheduled():
    """max_follow_ups is a stopping rule — it must not sit behind a delay
    window. A case out of follow-up budget has no next action to wait for."""
    case_id = _create_case_direct("invoice_overdue", 100_000,
        payload={"days_overdue": 5}, payment_rail="upi")
    run_pipeline(SessionLocal(), case_id)

    db = SessionLocal()
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    case.follow_up_count = POLICY["max_follow_ups"]        # budget spent
    case.scheduled_for = datetime.now(timezone.utc) + timedelta(hours=24)  # but scheduled
    case.status = CaseStatus.PAYMENT_PENDING
    db.commit(); db.close()

    run_follow_up_check(SessionLocal(), force=True)
    db = SessionLocal()
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    assert case.status == CaseStatus.ESCALATED, (
        "A case past max_follow_ups must escalate immediately, not wait out "
        "a scheduled window for a contact it will never make.")
    assert "FOLLOWUPS_EXHAUSTED" in _audit_types(case_id)
    db.close()
