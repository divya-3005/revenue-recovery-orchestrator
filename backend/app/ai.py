"""
AI Provider — structured output from Gemini (Feature 2 + Feature 4).

Uses google-genai SDK for structured JSON output.
Falls back to a mock when no API key is set (useful for local dev and tests).
"""

import os
import json
import logging
from typing import Type, TypeVar

from pydantic import BaseModel

from app.models import (
    DiagnosisResult, DecisionResult, RootCauseCategory, RecoveryActionType,
    CaseType, RecoveryCase,
)

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)


class AIProvider:
    """Wraps the Gemini API. If no API key is set, uses rule-based fallback."""

    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.client = None
        self.model_id = os.getenv("GEMINI_MODEL_ID", "gemini-2.5-flash")

        if self.api_key:
            try:
                from google import genai
                self.client = genai.Client(api_key=self.api_key)
            except Exception as e:
                logger.warning(f"Gemini SDK init failed: {e}. Using rule-based fallback.")

    def ask_structured(self, prompt: str, response_schema: Type[T]) -> T:
        """Send prompt to Gemini, get back a validated Pydantic model."""
        if not self.client:
            raise RuntimeError("No AI provider configured")

        response = self.client.models.generate_content(
            model=self.model_id,
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": response_schema,
            },
        )
        return response_schema.model_validate_json(response.text)


# ── Diagnosis (Feature 2) ───────────────────────────────────────────────

def diagnose(case: RecoveryCase, provider: AIProvider) -> DiagnosisResult:
    """Classify WHY a case is at risk. Returns structured diagnosis + confidence."""
    payload = case.raw_signal_payload or {}
    history = payload.get("payment_history", {})

    prompt = f"""You are an expert Razorpay payments and revenue recovery AI.
Analyze this revenue-at-risk case and diagnose the root cause.

[CASE DATA]
Type: {case.case_type.value}
Amount: {case.amount_paise} paise ({case.currency})
Payment Rail: {case.payment_rail or "Not specified"}
Customer Payment History: {json.dumps(history)}
Raw Signal Data: {json.dumps(payload)}

[INSTRUCTIONS]
1. root_cause_category — pick exactly one:
   - hard_decline: Permanent failure (stolen card, closed account). Retries will fail.
   - soft_decline: Temporary failure (insufficient funds, network error). Retries might work.
   - friction: User abandoned checkout (analyze: is_repeat_customer, abandoned_hour, cart value).
   - dispute: Customer initiated chargeback or contested invoice.
   - missed_payment: Invoice/link past due. Distinguish cash_flow_delay vs simply_missed vs disputed_invoice.
   - unknown: Insufficient evidence to classify.

2. specific_reason: Concise string (e.g., 'insufficient_funds', 'cash_flow_delay').
3. confidence_score: 0.0-1.0 based on evidence quality.
4. reasoning: 1-2 sentence plain-language explanation referencing actual data fields."""

    if provider.client:
        return provider.ask_structured(prompt, DiagnosisResult)
    return _fallback_diagnose(case)


def _fallback_diagnose(case: RecoveryCase) -> DiagnosisResult:
    """Rule-based diagnosis when no AI provider is available."""
    payload = case.raw_signal_payload or {}
    reason = payload.get("reason", "")

    if case.case_type == CaseType.SUBSCRIPTION_FAILED:
        hard_reasons = {"lost_card_reported", "stolen_card", "account_closed", "fraud_suspected"}
        if reason in hard_reasons:
            return DiagnosisResult(
                root_cause_category=RootCauseCategory.HARD_DECLINE,
                specific_reason=reason,
                confidence_score=0.95,
                reasoning=f"Hard decline: '{reason}' indicates permanent failure.",
            )
        return DiagnosisResult(
            root_cause_category=RootCauseCategory.SOFT_DECLINE,
            specific_reason=reason or "temporary_failure",
            confidence_score=0.85,
            reasoning=f"Soft decline: '{reason or 'unknown'}' is likely temporary and retryable.",
        )

    if case.case_type == CaseType.CHECKOUT_ABANDONED:
        return DiagnosisResult(
            root_cause_category=RootCauseCategory.FRICTION,
            specific_reason="checkout_drop_off",
            confidence_score=0.80,
            reasoning="Customer abandoned checkout — likely friction or distraction.",
        )

    if case.case_type == CaseType.INVOICE_OVERDUE:
        history = payload.get("payment_history", {})
        if history.get("on_time_ratio", 0) > 0.7:
            return DiagnosisResult(
                root_cause_category=RootCauseCategory.MISSED_PAYMENT,
                specific_reason="cash_flow_delay",
                confidence_score=0.82,
                reasoning="Customer has good payment history — likely temporary cash flow issue.",
            )
        return DiagnosisResult(
            root_cause_category=RootCauseCategory.MISSED_PAYMENT,
            specific_reason="simply_missed",
            confidence_score=0.75,
            reasoning="Invoice overdue with no strong payment history — appears simply missed.",
        )

    return DiagnosisResult(
        root_cause_category=RootCauseCategory.UNKNOWN,
        specific_reason="insufficient_data",
        confidence_score=0.50,
        reasoning="Insufficient signal data to classify root cause.",
    )


# ── Decision (Feature 4) ────────────────────────────────────────────────

def decide(case: RecoveryCase, diagnosis: DiagnosisResult, provider: AIProvider) -> DecisionResult:
    """Propose the next-best recovery action based on diagnosis and case state."""
    from app.policy import POLICY

    rail = (case.payment_rail or "").lower()
    is_mandate_rail = rail in ("enach", "nach", "emandate", "mandate")
    mandate_note = ""
    if is_mandate_rail:
        mandate_note = (
            f" IMPORTANT: this case is on the {case.payment_rail} rail — RBI regulations require "
            f"AT LEAST {POLICY['pre_debit_notice_hours']}h pre-debit notice, so delay_hours MUST be "
            f">= {POLICY['pre_debit_notice_hours']}."
        )

    total_attempts = case.retry_count + case.follow_up_count  # for attempt-staging guidance
    contact_count = case.contact_count  # for channel selection — excludes silent retries


    rejection_note = ""
    if case.last_rejection_note:
        rejection_note = f"""\n\n[REVIEWER FEEDBACK]\nA human reviewer rejected the previous proposal: \"{case.last_rejection_note}\"\nPropose a DIFFERENT action that addresses this objection."""

    prompt = f"""You are an expert payments and revenue recovery AI.
Based on the diagnosis and case state, propose the NEXT BEST recovery action.

[CASE STATE]
Amount: {case.amount_paise} paise
Payment Rail: {case.payment_rail or "Not specified"}
Failed Execution Attempts: {case.retry_count} (Max: {POLICY['max_retries']})
Follow-Up Passes (re-engaged after no payment): {case.follow_up_count}
Total Attempts So Far (includes silent retries): {total_attempts}
Customer Contacts So Far (real messages sent, excludes silent retries): {contact_count}
Cumulative Discount: {case.cumulative_discount_paise} paise

[DIAGNOSIS]
Category: {diagnosis.root_cause_category.value}
Reason: {diagnosis.specific_reason}
Confidence: {diagnosis.confidence_score}

[ACTIONS]
1. retry_charge — silently re-attempt the customer's saved payment method. No customer contact. Use this as the FIRST response to a fresh soft decline (Total Attempts So Far == 0) UNLESS the case is on an eNACH/NACH mandate rail — RBI pre-debit notice applies to ANY debit attempt on a mandate, so there's no such thing as an immediate silent retry there; use create_payment_link with the compliant delay instead. Params: {{}}.
2. switch_rail — move to a different, more reliable payment rail (e.g. card->upi). Use after one retry_charge has already failed on the current rail. Params: {{"target_rail": "upi"|"card"|"enach", "delay_hours": int, "channel": "email"|"sms"}}.{mandate_note}
3. create_payment_link — ask the customer to pay via a link. For soft declines (after retry_charge/switch_rail have been tried), abandoned checkouts, overdue invoices. Params: {{"delay_hours": int, "channel": "email"|"sms"}}.{mandate_note}
4. send_reminder — a lightweight nudge referencing an existing link/invoice, no new link created. Use for a LOW-VALUE first-contact abandoned checkout. Params: {{"channel": "email"|"sms"}}.
5. offer_discount — for friction/pricing drop-offs from a repeat customer or high-value cart. Params: {{"discount_percent": int}} (max {POLICY['max_discount_percent']}%)
6. escalate_to_human — for disputes, hard declines, high-value, unknown/low-confidence
7. stop — unrecoverable case (fraud confirmed)

Prefer "email" for a customer's first REAL contact and "sms" for any follow-up contact (Customer Contacts So Far >= 1) — a second nudge is more urgent. A silent retry_charge does NOT count as a contact.{rejection_note}

Output: recommended_action, action_parameters, confidence_score (0-1), reasoning (1-2 sentences)."""

    if provider.client:
        return provider.ask_structured(prompt, DecisionResult)
    return _fallback_decide(case, diagnosis)


def _pick_channel(contact_count: int) -> str:
    """First contact goes out over email; any re-engagement after that
    switches to SMS, which is more urgent and more likely to be read.
    contact_count is the number of customer-facing messages already sent,
    NOT total attempts (which includes silent retry_charge)."""
    return "email" if contact_count == 0 else "sms"


def _fallback_decide(case: RecoveryCase, diagnosis: DiagnosisResult) -> DecisionResult:
    """Rule-based decision when no AI provider is available."""
    from app.policy import POLICY

    cat = diagnosis.root_cause_category
    rail = (case.payment_rail or "").lower()
    is_mandate_rail = rail in ("enach", "nach", "emandate", "mandate")
    total_attempts = case.retry_count + case.follow_up_count
    # Use contact_count (customer-facing messages sent) for channel picking,
    # not total_attempts (which includes silent retry_charge).
    contact_count = case.contact_count
    channel = _pick_channel(contact_count)

    # If a human rejected the previous proposal, escalate rather than
    # re-proposing the same action.
    if case.last_rejection_note:
        return DecisionResult(
            recommended_action=RecoveryActionType.ESCALATE_TO_HUMAN,
            confidence_score=0.60,
            reasoning=f"Reviewer rejected the automated proposal ({case.last_rejection_note}) — "
                      f"routing to manual handling rather than re-proposing the same action.",
        )

    if cat == RootCauseCategory.HARD_DECLINE:
        return DecisionResult(
            recommended_action=RecoveryActionType.ESCALATE_TO_HUMAN,
            confidence_score=0.90,
            reasoning="Hard decline — cannot retry. Escalating for human review.",
        )

    if cat == RootCauseCategory.DISPUTE:
        return DecisionResult(
            recommended_action=RecoveryActionType.ESCALATE_TO_HUMAN,
            confidence_score=0.90,
            reasoning="Customer dispute — requires human intervention.",
        )

    if cat == RootCauseCategory.SOFT_DECLINE:
        if total_attempts == 0 and not is_mandate_rail:
            # First response to a fresh soft decline: silently retry the
            # saved payment method before ever bothering the customer.
            # NOT available on mandate rails (enach/nach) — RBI's
            # pre-debit notice window applies to any debit attempt on a
            # mandate, not just a customer-facing payment link, so
            # there's no such thing as an immediate *silent* retry there.
            # Those cases fall through to the compliant, delayed
            # create_payment_link below instead.
            return DecisionResult(
                recommended_action=RecoveryActionType.RETRY_CHARGE,
                confidence_score=0.85,
                reasoning="Soft decline, first attempt — silently retrying the saved payment method before contacting the customer.",
            )
        if total_attempts == 1 and rail in ("card", "upi"):
            # The silent retry didn't clear the case (it's back for another
            # pass) — try a different rail before asking for a brand-new
            # payment link on the one that just failed.
            target_rail = "upi" if rail == "card" else "card"
            delay = POLICY["pre_debit_notice_hours"] if target_rail in ("enach", "nach") else 24
            return DecisionResult(
                recommended_action=RecoveryActionType.SWITCH_RAIL,
                action_parameters={"target_rail": target_rail, "delay_hours": delay, "channel": channel},
                confidence_score=0.80,
                reasoning=f"Retry on {rail} didn't clear — switching to {target_rail}, a more reliable rail for this customer.",
            )
        delay = POLICY["pre_debit_notice_hours"] if is_mandate_rail else 24
        reasoning = (
            f"Soft decline on {case.payment_rail} — RBI pre-debit notice applies to any debit "
            f"attempt on this mandate, so using a compliant {delay}h delay instead of a silent retry."
            if is_mandate_rail else
            "Soft decline — creating payment link for customer-initiated retry."
        )
        return DecisionResult(
            recommended_action=RecoveryActionType.CREATE_PAYMENT_LINK,
            action_parameters={"delay_hours": delay, "channel": channel},
            confidence_score=0.85,
            reasoning=reasoning,
        )

    if cat == RootCauseCategory.FRICTION:
        payload = case.raw_signal_payload or {}
        # Mirror the real prompt's guidance ("offer_discount — for
        # friction/pricing drop-offs"): a repeat customer or high-value
        # cart is worth a discount; otherwise a plain nudge is enough
        # on first contact, escalating to a full payment link if the
        # customer still hasn't converted.
        cart_value = payload.get("cart_value", case.amount_paise)
        is_repeat = payload.get("is_repeat_customer", False)
        if is_repeat or cart_value >= 500_00:  # >= ₹500
            current_discount_pct = int(case.cumulative_discount_paise * 100 / case.amount_paise)
            headroom_pct = POLICY["max_discount_percent"] - current_discount_pct
            if headroom_pct >= 5:
                step_up = min(10, headroom_pct)
                new_discount_pct = current_discount_pct + step_up
                return DecisionResult(
                    recommended_action=RecoveryActionType.OFFER_DISCOUNT,
                    action_parameters={"discount_percent": new_discount_pct, "channel": channel},
                    confidence_score=0.80,
                    reasoning=f"Repeat customer / high-value cart — offering {new_discount_pct}% discount "
                              f"(stepped up by {step_up}% from previous best offer).",
                )
            elif current_discount_pct > 0:
                return DecisionResult(
                    recommended_action=RecoveryActionType.OFFER_DISCOUNT,
                    action_parameters={"discount_percent": current_discount_pct, "channel": channel},
                    confidence_score=0.80,
                    reasoning=f"Max discount reached — sending a final reminder with the best {current_discount_pct}% discount.",
                )
        if total_attempts == 0:
            return DecisionResult(
                recommended_action=RecoveryActionType.SEND_REMINDER,
                action_parameters={"channel": channel},
                confidence_score=0.75,
                reasoning="Low-value, first-time cart abandonment — a lightweight nudge before committing to a full payment link.",
            )
        return DecisionResult(
            recommended_action=RecoveryActionType.CREATE_PAYMENT_LINK,
            action_parameters={"delay_hours": 1, "channel": channel},
            confidence_score=0.80,
            reasoning="Checkout abandoned and the earlier reminder didn't convert — sending a payment link to recover the cart.",
        )

    if cat == RootCauseCategory.MISSED_PAYMENT:
        delay = POLICY["pre_debit_notice_hours"] if is_mandate_rail else 24
        reasoning = (
            f"Invoice overdue on {case.payment_rail} — using RBI-compliant {delay}h pre-debit delay."
            if is_mandate_rail else
            "Invoice overdue — sending payment link to recover funds."
        )
        return DecisionResult(
            recommended_action=RecoveryActionType.CREATE_PAYMENT_LINK,
            action_parameters={"delay_hours": delay, "channel": channel},
            confidence_score=0.80,
            reasoning=reasoning,
        )

    return DecisionResult(
        recommended_action=RecoveryActionType.ESCALATE_TO_HUMAN,
        confidence_score=0.50,
        reasoning="Unknown root cause — escalating for human review.",
    )
