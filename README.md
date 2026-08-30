# Revenue Recovery Orchestrator

An autonomous, AI-driven Revenue Recovery Orchestrator that intercepts failed subscription payments, checkout drop-offs, and overdue invoices to recover lost revenue automatically with strict deterministic safety guardrails.

---

## 1. What It Does

When a transaction or checkout fails, the orchestrator:
1. Ingests the revenue-risk signal into a single canonical `RecoveryCase`.
2. Runs **AI Diagnosis** to categorize the root cause without hallucinating customer history.
3. Proposes the **Next Best Action** (e.g., `SEND_REMINDER`, `OFFER_DISCOUNT`, `ESCALATE`, `STOP`).
4. Evaluates strict **Deterministic Safety Guardrails** (hard declines blocked from retries, max 3 attempts, discount caps <= 15%, human approval for amounts > ₹50,000).
5. Generates **Diagnosis-Specific Customer Communication** across Email, SMS, and WhatsApp.
6. Executes recovery via **Razorpay Test Mode** payment links (or mock channels).
7. Transitions state upon verified **Payment Confirmation** while maintaining an append-only **Audit Trail**.

---

## 2. Architecture & Pipeline

```text
Revenue Risk Signal (Webhook / Beacon / Ingest)
                    ↓
        Canonical RecoveryCase
                    ↓
        AI Diagnosis (LLM Agent)
                    ↓
    Next Best Action Recommendation
                    ↓
      Deterministic Policy Check ──[Violated / High-Value]──→ Human Approval Gate (AWAITING_APPROVAL)
                    ↓ [Passed]                                       ↓ [Approved]
      Customer Message Generation                                    ↓
                    ↓                                                ↓
     Execution (Razorpay / Link) ←────────────────────────────────────┘
                    ↓
    Awaiting Payment (PAYMENT_PENDING)
                    ↓
  Verified Payment Confirmation (Webhook)
                    ↓
               RECOVERED
```

---

## 3. Supported Input Signals

All three signals are normalized into the same canonical `RecoveryCase` model:

1. **Subscription / Mandate Failure** (`subscription_failed`):
   - Ingested via Razorpay webhook (`payment.failed` / `subscription.pending`) or direct API.
   - Example: Insufficient funds, expired card, bank timeouts.
2. **Abandoned Checkout** (`checkout_abandoned`):
   - Ingested via idle beacon ping or checkout drop-off event.
   - Example: High cart friction, exit intent, repeat visitor cart retention.
3. **Overdue Invoice** (`invoice_overdue`):
   - Ingested via payment link expiration webhook or billing signal.
   - Example: Unpaid milestone invoices, cash-flow delays.

---

## 4. AI Diagnosis & Next-Best Action

- **AI Diagnosis**: Produces structured JSON classification (`category`, `reason`, `confidence`, `explanation`).
- **Next Best Action Enum**:
  - `SEND_REMINDER` (creates Razorpay payment link + delivers tailored message)
  - `OFFER_DISCOUNT` (creates Razorpay payment link with capped discount applied)
  - `ESCALATE` (routes case to human review queue)
  - `STOP` (halts recovery when unrecoverable or opted out)
- **AI Safety Rule**: The AI *never* directly executes financial transactions or moves funds. Its recommendations must pass deterministic policy validation first.

---

## 5. Deterministic Policy Guardrails

Policy checks are written in pure Python code and executed before every action:
- **No Hard Decline Retries**: Stolen cards, closed accounts, and fraud flags are never retried.
- **Max Retries Capped**: Maximum 3 automated recovery attempts per case.
- **Discount Limit**: Maximum 15% discount cap.
- **High-Value Gate**: Transactions exceeding ₹50,000 require human review and cannot be executed automatically.
- **Low-Confidence Safeguard**: Confidence below 0.60 automatically routes to human escalation.
- **Strict Approval Token**: Human approval cryptographically verifies `pending_decision_id` and `pending_decision_hash` and cannot bypass hard safety rules.

---

## 6. Customer Communication

- Tailors messaging tone based on attempt count (Gentle → Firm → Final).
- Diagnosis-specific copy for Insufficient Funds, Expired Card, Cart Drop-Off, and Overdue Invoice.
- **Honest Delivery Reporting**: Real external SMS and WhatsApp gateways are marked as `Simulated Delivery` in development mode, clearly identified in the UI and audit logs.

---

## 7. Setup & Running Locally

### Prerequisites
- Python 3.11+
- Node.js 20+
- Docker & Docker Compose (optional, for Postgres)

### Backend Setup
```bash
cd backend
python -m pip install -r requirements.txt

# Run migrations
alembic upgrade head

# Start backend server
uvicorn app.main:app --reload --port 8000
```

### Frontend Setup
```bash
cd frontend
npm install
npm run dev
```
Open `http://localhost:3000` to access the dashboard.

---

## 8. Running Automated Tests

Run the complete test suite:
```bash
cd backend
pytest -v
```
All 73 unit, integration, invariant, and workflow tests will run and pass cleanly.

---

## 9. Demo Scenarios

You can seed and test the 5 canonical demo scenarios by clicking **"Seed 5 Demo Scenarios"** on the dashboard or calling:
```bash
curl -X POST http://localhost:8000/api/v1/demo/seed
```

1. **Scenario 1 — Recoverable Soft Decline**: ₹4,999 insufficient funds → soft decline diagnosis → reminder with payment link → policy allows → payment confirmed → `RECOVERED`.
2. **Scenario 2 — Hard Decline**: ₹2,999 stolen/lost card → hard decline diagnosis → policy blocks retry → `STOPPED`.
3. **Scenario 3 — Abandoned Checkout**: ₹2,999 cart with repeat visitor → checkout friction diagnosis → reminder with payment link.
4. **Scenario 4 — High-Value Human Approval**: ₹75,000 subscription failure → policy requires human approval → moves to `AWAITING_APPROVAL` → human approves via UI → execution triggered.
5. **Scenario 5 — Overdue Invoice**: ₹15,000 overdue invoice (7 days past due) → missed payment diagnosis → reminder with payment link.

---

## 10. Known Limitations

- **Simulated Comms**: SMS and WhatsApp delivery modes are simulated for development; in production, SMS/WhatsApp gateways (e.g. Twilio/Gupshup) can be plugged into `comms.py`.
- **Native Charge Retry**: Direct server-to-server card auto-retry without customer redirection is restricted in Razorpay Test Mode; the orchestrator issues test-mode Razorpay Payment Links for customer-authorized settlement.
