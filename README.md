# Revenue Recovery Orchestrator

AI-powered recovery system for three types of revenue at risk:
- **Failed subscription/mandate charges** (soft & hard declines)
- **Abandoned checkouts** (friction signals)
- **Overdue invoices/payment links** (missed payments)

One orchestrator, one decision engine, one policy layer — not three separate scripts.

## Architecture

```
Signal → Case Creation → AI Diagnosis → AI Decision → Policy Check → Comms → Execute → Track
              ↑                                              ↓
              └──────────────── Retry Loop ←─────────────────┘
```

**Key files:**

| File | Purpose |
|------|---------|
| `app/main.py` | FastAPI routes — all API endpoints |
| `app/pipeline.py` | The recovery pipeline (diagnosis → decision → policy → execute) |
| `app/models.py` | Database tables + Pydantic domain types |
| `app/ai.py` | Gemini AI provider (with rule-based fallback) |
| `app/policy.py` | Deterministic guardrail rules |
| `app/comms.py` | Customer message templates |
| `app/executor.py` | Razorpay SDK executor (payment links) |
| `app/synthetic.py` | Batch synthetic case generation |

## Quick Start

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload
```

The app starts immediately with SQLite — no database setup needed.

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| POST | `/api/v1/cases` | Ingest a new recovery case |
| GET | `/api/v1/cases` | List all cases (sorted by priority) |
| GET | `/api/v1/cases/{id}/audit` | Full audit trail for a case |
| GET | `/api/v1/cases/escalated` | Cases needing human review |
| POST | `/api/v1/cases/{id}/approve` | Approve an escalated action |
| POST | `/api/v1/cases/{id}/reject` | Reject and re-evaluate |
| POST | `/api/v1/cases/{id}/close` | Close an escalated case |
| POST | `/api/v1/cases/{id}/promise-to-pay` | Capture promise-to-pay |
| POST | `/api/v1/cases/{id}/opt-out` | Customer opt-out |
| POST | `/api/v1/batch` | Run 50+ synthetic cases |
| GET | `/api/v1/analytics` | Recovery analytics |
| GET | `/api/v1/policy` | Policy configuration |
| POST | `/api/v1/demo/seed` | Seed 5 demo scenarios |
| POST | `/api/v1/demo/confirm-payment/{id}` | Simulate payment |

## Running Tests

```bash
cd backend
python -m pytest tests/ -v
```

## Configuration

Set these environment variables for production features (optional for local dev):

| Variable | Purpose | Default |
|----------|---------|---------|
| `GEMINI_API_KEY` | Gemini AI for diagnosis/decisions | Rule-based fallback |
| `RAZORPAY_KEY_ID` | Razorpay test-mode key | Dry-run mode |
| `RAZORPAY_KEY_SECRET` | Razorpay test-mode secret | Dry-run mode |
| `DATABASE_URL` | Database connection string | `sqlite:///recovery.db` |

## Features

1. **Revenue-at-Risk Detection** — Three signal types normalized into one case format
2. **AI Root-Cause Diagnosis** — Structured classification with confidence scores
3. **Guardrail & Policy System** — Deterministic rules bounding every action
4. **Next-Best-Action Decision** — AI-driven recovery action selection
5. **Multi-Channel Execution** — Razorpay payment links via real test-mode APIs
6. **Customer Communications** — Personalized messages with tone escalation
7. **Case Tracking & Re-loop** — Retry logic with memory
8. **Stopping Rules** — Max retries, max days, opt-out, hard-decline blocks
9. **Escalation to Human** — Full-context handoff queue
10. **Audit Trail** — Complete timestamped log per case
11. **Batch Processing** — 50+ cases processed end-to-end
12. **Recovery Analytics** — Dashboard with recovery rates and economics
13. **Recovery Prioritization** — Expected-value ranking
14. **Promise-to-Pay** — Capture commitment, suppress reminders, auto-escalate
15. **Human Approval Gate** — Click-to-approve for high-risk actions
16. **Unified Orchestrator** — One engine for subscriptions, checkouts, and invoices
17. **Net Recovery Economics** — Revenue minus discounts and comms costs
18. **Explainable Decisions** — Full reasoning trace per case
