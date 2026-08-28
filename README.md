# Revenue Recovery Orchestrator

This is an autonomous, AI-driven Revenue Recovery Orchestrator that intercepts failed payments, checkout drop-offs, and overdue invoices to recover lost revenue automatically. It uses an autonomous AI agent to diagnose the root cause of the failure, propose a recovery action, validate it against strict deterministic guardrails, and execute it (via Razorpay Test Mode or internal channels) while persisting an immutable audit trail.

## Pipeline Architecture

The recovery pipeline follows a clear, durable workflow for each case:
1. **Signal**: An external event is received via webhook or batch ingestion (e.g., `payment.failed`, `subscription.pending`). A Case is created.
2. **Diagnosis**: An LLM agent (Gemini with Groq fallback) diagnoses the root cause (e.g., hard decline, soft decline, friction).
3. **Guardrail-Bound Decision**: The agent decides the best recovery action (e.g., retry charge, send reminder, offer discount, escalate, stop).
4. **Policy Check**: The AI's decision is validated against deterministic rules (e.g., max retries, discount caps, value limits). Actions violating policies are rejected or escalated.
5. **Execution**: A `RazorpayExecutor` invokes Razorpay's API to execute the approved action (e.g., creating a new payment link with a discount or retry).
6. **Result**: The system determines if the recovery succeeded, needs retries, or failed.
7. **Audit**: Every step (Diagnosis, Decision, Policy, Comms, Execution) checkpoints to an immutable Postgres audit trail table.
8. **Analytics**: Real-time aggregated data tracking value at risk vs recovered.

## Key Technology Choices

- **Inngest**: Chosen for durable, retriable background step executions (`step.run()`). If the process crashes during an LLM call or API call, Inngest resumes safely without duplicating work.
- **Postgres + JSONB**: Provides transactional integrity for state transitions and rich unstructured storage (JSONB) for webhook payloads and AI reasoning. The audit trail uses deterministic IDs to enforce idempotency natively at the DB level.
- **Gemini + Groq Fallback**: Used for high-quality, cost-effective reasoning. The primary calls go to Gemini, with an automatic transparent fallback to Groq to guarantee high availability during provider outages.
- **FastAPI**: Provides a high-performance asynchronous API for ingesting signals and serving the dashboard.

## Setup Instructions

1. Ensure Docker and Docker Compose are installed.
2. Copy `.env.example` to `.env` inside the `backend` directory (or use `.env.example` as a reference to set environment variables). 
   You must provide:
   - `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET`
   - `RAZORPAY_WEBHOOK_SECRET`
   - `GEMINI_API_KEY` / `GROQ_API_KEY`
3. Run `docker-compose up -d --build`. This will automatically run database migrations (Alembic) and start the Postgres and API services.
4. The dashboard is available at `http://localhost:8000/`.

## Triggering Data

You can trigger a batch run of synthetic cases spanning various failure reasons to see the AI agent and the pipeline in action:
```bash
curl -X POST http://localhost:8000/api/v1/batch
```
After running this, refresh the dashboard to view the generated cases, watch them transition through states, and click "View Trail" to see the AI reasoning and execution outcome for each case.
