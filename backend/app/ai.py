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

    prompt = f"""You are an expert payments and revenue recovery AI.
Based on the diagnosis and case state, propose the NEXT BEST recovery action.

[CASE STATE]
Amount: {case.amount_paise} paise
Retry Count: {case.retry_count} (Max: {POLICY['max_retries']})
Cumulative Discount: {case.cumulative_discount_paise} paise

[DIAGNOSIS]
Category: {diagnosis.root_cause_category.value}
Reason: {diagnosis.specific_reason}
Confidence: {diagnosis.confidence_score}

[ACTIONS]
1. create_payment_link — for soft declines, abandoned checkouts, overdue invoices. Params: {{"delay_hours": int}}
2. offer_discount — for friction/pricing drop-offs. Params: {{"discount_percent": int}} (max {POLICY['max_discount_percent']}%)
3. send_reminder — for missed payments, nudges. Params: {{"channel": "email"|"sms"|"whatsapp"}}
4. switch_rail — when payment rail failed, try alternative. Params: {{"target_rail": "upi"|"card"|"enach", "channel": "email"}}
5. escalate_to_human — for disputes, hard declines, high-value, unknown/low-confidence
6. stop — unrecoverable case (fraud confirmed)

Output: recommended_action, action_parameters, confidence_score (0-1), reasoning (1-2 sentences)."""

    if provider.client:
        return provider.ask_structured(prompt, DecisionResult)
    return _fallback_decide(case, diagnosis)


def _fallback_decide(case: RecoveryCase, diagnosis: DiagnosisResult) -> DecisionResult:
    """Rule-based decision when no AI provider is available."""
    cat = diagnosis.root_cause_category

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
        return DecisionResult(
            recommended_action=RecoveryActionType.CREATE_PAYMENT_LINK,
            action_parameters={"delay_hours": 24},
            confidence_score=0.85,
            reasoning="Soft decline — creating payment link for customer-initiated retry.",
        )

    if cat == RootCauseCategory.FRICTION:
        return DecisionResult(
            recommended_action=RecoveryActionType.CREATE_PAYMENT_LINK,
            action_parameters={"delay_hours": 1},
            confidence_score=0.80,
            reasoning="Checkout abandoned — sending payment link to recover cart.",
        )

    if cat == RootCauseCategory.MISSED_PAYMENT:
        return DecisionResult(
            recommended_action=RecoveryActionType.SEND_REMINDER,
            action_parameters={"channel": "email"},
            confidence_score=0.80,
            reasoning="Invoice overdue — sending payment reminder.",
        )

    return DecisionResult(
        recommended_action=RecoveryActionType.ESCALATE_TO_HUMAN,
        confidence_score=0.50,
        reasoning="Unknown root cause — escalating for human review.",
    )
