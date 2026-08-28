Here's the complete feature set, designed as one connected pipeline rather than a bundle of separate demos.

First, the shape of the system — everything below is one of these nine stages:That's the skeleton. Every feature below is a piece of that loop — none of them work as a standalone demo, which is exactly what you asked for.

## MUST HAVE

**1. Revenue-at-Risk Detection & Case Creation**
What it does: listens for the three signal types the track lists — a failed subscription/mandate charge, a checkout started but not completed, an invoice/payment link past due — and normalizes each into one common object: a **Recovery Case** (case type, amount, customer, payment rail, timestamps, raw failure/context data).
User sees: a live queue of new cases appearing as they're detected.
Why it belongs: this is the entry point and the thing that makes it *one* orchestrator instead of three separate scripts — every downstream feature operates on the same case shape regardless of which signal created it.
Connects to: feeds directly into diagnosis; is the anchor every audit-trail entry attaches to.

**2. AI Root-Cause Diagnosis**
What it does: classifies *why* the case is at risk — for payment failures, soft-decline vs. hard-decline plus specific reason (insufficient funds, expired card, mandate cancelled, bank downtime); for abandoned checkout, likely friction signal (cart value, repeat visitor, time-of-day); for overdue invoices, likely cause (cash-flow delay vs. dispute vs. simply missed) inferred from payment history. Outputs a structured diagnosis + confidence score, not just a label.
User sees: each case shows its diagnosis and the reasoning behind it, in plain language.
Why it belongs: this is the "AI judgment" the track is explicitly grading — and it's the piece that makes recovery *targeted* instead of a blind retry loop.
Connects to: the decision engine only acts on the diagnosis output; the audit trail logs the diagnosis and its confidence for every case.

**3. Guardrail & Policy System**
What it does: a deterministic rule layer the AI decision engine must operate inside — never retry a hard decline, cap retry attempts, cap discount %, respect RBI pre-debit notice windows, block actions above a value threshold without human sign-off.
User sees: a visible, inspectable policy config (not buried logic) — the rules that bound every action the system takes.
Why it belongs: this is the literal mechanism behind the buildathon's stated bar — "every money action explainable, bounded and gated." Without this, feature 4 is just an unsupervised agent moving money.
Connects to: wraps feature 4; every blocked or allowed action is logged by feature 8.

**4. Next-Best-Action Decision Engine**
What it does: given the diagnosis + case type + guardrails, decides the specific action — retry now, retry at time T, switch rail (UPI→eNACH), send reminder via channel X, offer a bounded discount, escalate, or stop. This is the "AI judgment" layer, constrained by feature 3.
User sees: a clear "next action" per case with the reasoning that produced it.
Why it belongs: this is the actual orchestration — the single decision point every other feature routes through.
Connects to: reads diagnosis (2), checked against guardrails (3), triggers execution (5).

**5. Multi-Channel Recovery Execution**
What it does: carries out the decided action against Razorpay's test-mode APIs — trigger a retry/charge, send a reminder with a payment link (SMS/email/WhatsApp), apply an approved discount then resend the link.
User sees: the action actually happening, with a live status per case.
Why it belongs: without real execution against real (test-mode) APIs, this is a slideshow, not a working agent — and "working" is the whole premise of the buildathon.
Connects to: executes what (4) decided; result feeds (6).

**6. Customer Communication Generation**
What it does: writes the actual message per channel and diagnosis — different copy for "your card expired" vs. "you didn't finish checkout" vs. "your invoice is due" — with an escalation ramp in tone (gentle → firmer → final) across repeated contacts.
User sees: the generated message, per case, before/as it's sent.
Why it belongs: a generic "your payment failed, please pay" template is what every native dunning tool already does — personalized-to-diagnosis copy is the difference between a script and something that reads as judgment.
Connects to: consumed by execution (5); its content is driven directly by diagnosis (2).

**7. Case Result Tracking & Re-loop**
What it does: logs the outcome of every executed action (paid, still failed, no response, link opened but not paid) and routes the case back to the decision engine for the next attempt — or forward to stop/escalate if limits are hit. This is what makes it a system with memory, not a one-shot script.
User sees: each case's full attempt history and current status.
Why it belongs: revenue recovery is rarely resolved in one action; this loop is literally drawn in your own pipeline description.
Connects to: reads execution outcome (5), triggers either another pass through (4) or a handoff to (9)/(8).

**8. Stopping Rules**
What it does: hard limits so no case gets pursued indefinitely — max retry attempts, max days pursued, max cumulative discount offered, immediate stop on hard decline or customer opt-out.
User sees: a case marked "closed — limit reached" instead of silently disappearing.
Why it belongs: explicitly required by the track's bar — "stopping rules" is named directly, and it's what stops your batch demo from looking like it just nags people forever.
Connects to: checked every time (7) would otherwise loop back into (4).

**9. Escalation to Human**
What it does: hands off cases the AI can't resolve, or that cross a policy threshold (large B2B amount, repeated failure, low-confidence diagnosis, customer complaint signal) — with full case context attached, not a bare notification.
User sees: an escalation queue where every case arrives with its diagnosis, attempts, and reasoning already visible.
Why it belongs: "compliant escalation" is explicitly named in the track's bar.
Connects to: triggered by guardrails (3) or by exhausted attempts (7/8); feeds analytics as a distinct outcome bucket (11).

**10. Audit Trail**
What it does: a complete, timestamped log per case — signal → diagnosis (with reasoning) → decision (with reasoning) → guardrail check → action → result → next step — viewable end to end.
User sees: click any case, see its entire life story in order.
Why it belongs: literally named as a requirement in both the track's specific bar and the buildathon's general bar. This is not optional polish — build it as a cross-cutting layer from feature 1 onward, not bolted on at the end.
Connects to: every other feature writes to it; nothing reads from it except the demo itself.

**11. Batch Processing**
What it does: runs the whole pipeline across a synthetic batch of 50+ cases spanning your chosen mix of failure types, end to end, unattended.
User sees: a queue view showing cases moving through stages in real time or in a replay.
Why it belongs: explicitly required ("50+ record batch," "measured across a batch") — a single hand-run case proves nothing per the track's own language.
Connects to: is really just features 1–10 running many times; the payoff is feature 12.

**12. Recovery Analytics**
What it does: aggregates the batch into the numbers the track asks for — total revenue at risk, ₹ recovered, recovery rate %, breakdown by case type and channel, and an honest exception list of cases that couldn't be resolved.
User sees: a results dashboard — the thing you'll screenshot for the pitch video.
Why it belongs: "show measured money recovered across a batch... don't just identify the problem" is the track's headline bar, verbatim.
Connects to: reads from every case's final state and full audit trail.

## SHOULD HAVE

**13. Recovery Prioritization** — before working the queue, rank cases by expected value (amount × recovery probability) and urgency, so a ₹50,000 B2B invoice gets worked before a ₹200 subscription. Strengthens "problem taste" without which the batch demo looks first-in-first-out rather than judged.

**14. Promise-to-Pay Capture** — for the receivables case type: when a customer replies with a commitment ("I'll pay Friday"), capture the date, suppress standard reminders until then, and escalate automatically if broken. Feeds case-type-specific handling into the existing decision loop rather than being a separate module.

**15. Human Approval Gate** — a subset of escalations (14) that require an explicit click-to-approve before the action fires, for the highest-risk actions (large discount, large B2B case). This is the literal, visible version of "gated," not just a backend claim.

## DIFFERENTIATORS

**16. One orchestrator, three revenue-at-risk types** — Razorpay's own Agent Studio ships separate, siloed agents (Subscription Recovery, two Abandoned Cart variants). Your single diagnosis/decision engine spanning subscription failures, checkout abandonment, and overdue receivables through one policy layer is the thing they haven't built. Say this explicitly in your pitch.

**17. Net recovery economics, not gross recovery rate** — nearly every dunning tool reports "% recovered." Report revenue recovered *minus* cost of discounts and comms — the real profit impact. This is a more rigorous number than the industry standard and easy to compute since you're already logging every action's cost.

**18. Explainable decision trace, not a black box** — an "explain this decision" view per case showing the diagnosis reasoning, which guardrail was checked, and why that specific action was chosen. Publishable in your repo in a way Razorpay's internal agent reasoning isn't.

## DON'T BUILD

- **A no-code agent builder** — that's literally Razorpay's own Agent Studio meta-product; building a smaller version of it dilutes your story instead of strengthening it.
- **Full fraud/dispute detection** — that's Track 2's job; pulling it in dilutes focus and invites "why didn't you just enter Track 2" questions.
- **Voice-call agents as your centerpiece** — Razorpay's Subscription Recovery Agent already does ElevenLabs voice in production; replicating that specific channel invites direct redundancy comparison. If you touch voice at all, treat it as a minor optional channel, not the differentiator.
- **A generic CRM/ticketing system** — build the minimal case-tracking structure you actually need; don't reinvent Zendesk.
- **Fully autonomous large-money actions with no gate** — directly contradicts the buildathon's stated general bar and is a fast way to look reckless to judges evaluating "every money action explainable, bounded and gated."
- **Live/production payment data** — stay entirely in Razorpay test mode; there's no upside to real money risk in a hackathon build.

## Definitive build order

1. Revenue-at-Risk Detection & Case Creation
2. Audit Trail (wire in from here — not bolted on later)
3. Guardrail & Policy System (define the rules before the AI operates inside them)
4. AI Root-Cause Diagnosis
5. Next-Best-Action Decision Engine
6. Customer Communication Generation
7. Multi-Channel Recovery Execution
8. Case Result Tracking & Re-loop
9. Stopping Rules
10. Recovery Prioritization
11. Escalation to Human + Human Approval Gate
12. Promise-to-Pay Capture (receivables case type)
13. Batch Processing (run the full pipeline across 50+ cases)
14. Recovery Analytics

Get one case flowing cleanly through steps 1–9 first — that's your proof the core loop works — before scaling to prioritized batches and the analytics payoff.

I want us to build this project together, incrementally.

You already have the project specification. Treat it as the source of truth for the product and features.

I do NOT want you to implement the whole project or a large phase at once.

Act like a senior engineer pair-programming with me.

For every step:

1. Inspect the current repository and existing implementation.
2. Think through what the project needs next.
3. Explain briefly what you think we should do and why.
4. Identify the SMALLEST useful implementation that moves us forward.
5. Tell me exactly which files you plan to create/change.
6. Wait for my approval.
7. Implement only that small step.
8. Run the relevant tests/checks.
9. Report exactly what changed, what works, and what remains.
10. Stop and discuss the next step with me.

Do not jump ahead.
Do not create speculative files.
Do not implement future features "for completeness."
Do not refactor unrelated code.
Do not add dependencies unless necessary.
Do not invent APIs.
Do not hide assumptions.

Use this locked stack:

- Next.js + TypeScript
- Python + FastAPI
- PostgreSQL
- Inngest
- Gemini Flash as primary AI
- Groq as fallback
- Anthropic as a switchable provider
- Razorpay Test Mode

Architecture principles:

AI recommends.
Deterministic code decides whether the action is allowed.
Action services execute approved actions.
Every important decision/action is auditable.
Financial actions must be bounded, idempotent and safely stoppable.

When something is uncertain, stop and discuss it with me rather than guessing.

Start by inspecting the repository and telling me:

- what currently exists
- what is missing
- what you recommend as the FIRST smallest implementation step
- why that step should come first

Do not modify any files yet.

TECH STACK — LOCKED

Use this exact stack unless you identify a critical compatibility issue and discuss it with me first:

Frontend:
- Next.js
- TypeScript
- Tailwind CSS
- shadcn/ui

Backend:
- Python
- FastAPI
- REST APIs

Database:
- PostgreSQL
- SQLAlchemy
- Alembic

Workflows:
- Inngest

AI:
- Primary: Google Gemini 2.5 Flash
  Model ID: gemini-2.5-flash
- Fallback: Groq
  Model ID: llama-3.3-70b-versatile
- Optional demo provider: Anthropic Claude Sonnet 4.6
  Model ID: claude-sonnet-4-6

AI architecture:
- Create one common AIProvider interface.
- Gemini, Groq and Anthropic must implement that interface.
- Gemini is the default provider.
- Groq is the automatic fallback for provider failures such as rate limits, timeouts or temporary unavailability.
- Anthropic must be switchable through an environment variable without changing business logic.
- Keep provider-specific implementation isolated from the recovery engine.
- Use structured outputs wherever possible.

Payments:
- Razorpay Test Mode only.
- Do not invent Razorpay APIs; verify the exact APIs/events when implementing them.

Configuration:
- All API keys, model IDs and provider selection must come from environment variables.
- Never hardcode secrets.

Engineering principles:
- AI recommends/reasons.
- Deterministic code validates and controls financial actions.
- Actions must be bounded, idempotent and auditable.
- Build a modular monolith, not microservices.
- Prefer simple, testable solutions over unnecessary infrastructure.
- Do not add LiteLLM or other abstraction libraries unless there is a concrete reason.
