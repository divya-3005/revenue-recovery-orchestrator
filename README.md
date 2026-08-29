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
2. Copy `backend/.env.example` to `backend/.env` and provide your API keys:
   - `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET`
   - `RAZORPAY_WEBHOOK_SECRET`
   - `GEMINI_API_KEY` / `GROQ_API_KEY`
3. Run `docker-compose up -d --build`. This will start:
   - PostgreSQL (Database)
   - FastAPI Backend (Port 8000)
   - Next.js Frontend Dashboard (Port 3000)
4. Start the local Inngest dev server to run the workflows (in a new terminal):
   ```bash
   npx inngest-cli@latest dev
   ```
5. Open `http://localhost:3000` in your browser to view the Revenue Recovery Dashboard.

## Triggering Data

You can trigger a batch run of synthetic cases spanning various failure reasons by clicking the **"Run 50+ Batch"** button on the dashboard, or via terminal:
```bash
curl -X POST http://localhost:8000/api/v1/batch
```
After running this, the cases will flow through the pipeline. You can watch them transition through states on the dashboard, and click **"View Trace"** to see the AI reasoning and execution outcome for each case.
