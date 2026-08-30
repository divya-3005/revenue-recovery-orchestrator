"""
Recovery Action Executor (Feature 5).

Executes recovery actions against Razorpay's test-mode APIs.
Falls back to dry-run mode when the SDK is unavailable.
"""

import os
import logging
import importlib
from typing import Any

from app.models import (
    RecoveryCase, DecisionResult, ExecutionResult, ExecutionStatus, RecoveryActionType,
)

logger = logging.getLogger(__name__)


class Executor:
    """
    Executes recovery actions. Uses Razorpay SDK for real payment link creation
    when configured, otherwise returns dry-run results.
    """

    def __init__(self):
        key_id = os.getenv("RAZORPAY_KEY_ID", "")
        key_secret = os.getenv("RAZORPAY_KEY_SECRET", "")

        # Safety: only allow test-mode keys
        if key_id and not key_id.startswith(("rzp_test_", "test_")):
            raise RuntimeError(f"RAZORPAY_KEY_ID must be test mode (got '{key_id[:10]}...')")

        self.client = None
        if key_id:
            try:
                razorpay = importlib.import_module("razorpay")
                self.client = razorpay.Client(auth=(key_id, key_secret))
            except Exception as e:
                logger.warning(f"Razorpay SDK unavailable: {e}. Using dry-run mode.")

    def execute(self, case: RecoveryCase, decision: DecisionResult) -> ExecutionResult:
        """Execute the approved recovery action."""
        action = decision.recommended_action

        # Internal actions — no external API call needed
        if action == RecoveryActionType.ESCALATE_TO_HUMAN:
            return ExecutionResult(
                status=ExecutionStatus.SUCCESS, action_taken=action,
                reason="Case escalated to human review queue.",
            )

        if action == RecoveryActionType.STOP:
            return ExecutionResult(
                status=ExecutionStatus.SUCCESS, action_taken=action,
                reason="Case closed — recovery stopped.",
            )

        # All other actions create a payment link
        return self._create_payment_link(case, decision)

    def _create_payment_link(self, case: RecoveryCase, decision: DecisionResult) -> ExecutionResult:
        """Create a Razorpay payment link (or dry-run if SDK unavailable)."""
        action = decision.recommended_action
        params = decision.action_parameters

        # Calculate amount (apply discount if applicable)
        amount = case.amount_paise
        if action == RecoveryActionType.OFFER_DISCOUNT:
            pct = params.get("discount_percent", 0)
            discount_paise = int(amount * pct / 100)
            amount -= discount_paise

        # Build description
        if action == RecoveryActionType.OFFER_DISCOUNT:
            desc = f"Discounted payment for case {case.id} ({params.get('discount_percent', 0)}% off)"
        elif action == RecoveryActionType.SWITCH_RAIL:
            desc = f"Payment via {params.get('target_rail', 'upi').upper()} for case {case.id}"
        else:
            desc = f"Recovery payment for case {case.id}"

        # Extract customer contact
        email = case.customer_email or (case.raw_signal_payload or {}).get("email")
        phone = case.customer_phone or (case.raw_signal_payload or {}).get("contact")
        if not email:
            email = f"{case.customer_id}@example.com"

        # Idempotency key based on case + retry count
        ref_id = f"{case.id[:20]}_{case.retry_count}"

        # Dry-run mode
        if not self.client:
            result_params = dict(params)
            result_params["amount_charged_paise"] = amount
            if action == RecoveryActionType.OFFER_DISCOUNT:
                result_params["discount_applied_paise"] = case.amount_paise - amount
            return ExecutionResult(
                status=ExecutionStatus.DRY_RUN, action_taken=action,
                reason="Dry run — Razorpay SDK not configured.",
                action_parameters_used=result_params,
            )

        # Real Razorpay API call
        try:
            link_data: dict[str, Any] = {
                "amount": amount,
                "currency": case.currency,
                "description": desc,
                "reference_id": ref_id,
                "customer": {"email": email, "contact": phone},
                "notes": {"case_id": case.id, "action": action.value},
            }

            # Set notification channel
            channel = params.get("channel", "email")
            if channel == "sms":
                link_data["notify"] = {"sms": 1, "email": 0}
            elif channel != "whatsapp":  # WhatsApp not natively supported by Razorpay
                link_data["notify"] = {"email": 1, "sms": 0}

            payment_link = self.client.payment_link.create(data=link_data)

            result_params = dict(params)
            result_params["amount_charged_paise"] = amount
            if action == RecoveryActionType.OFFER_DISCOUNT:
                result_params["discount_applied_paise"] = case.amount_paise - amount

            return ExecutionResult(
                status=ExecutionStatus.SUCCESS, action_taken=action,
                reason="Payment link created successfully.",
                external_reference_id=payment_link.get("id"),
                action_parameters_used=result_params,
            )
        except Exception as e:
            # Handle idempotency collision (link already exists)
            if "reference_id" in str(e).lower() and "already exists" in str(e).lower():
                return ExecutionResult(
                    status=ExecutionStatus.SUCCESS, action_taken=action,
                    reason="Payment link already exists (idempotency recovered).",
                    external_reference_id="idempotent",
                )
            return ExecutionResult(
                status=ExecutionStatus.FAILED, action_taken=action,
                reason=f"Razorpay API error: {e}",
            )
