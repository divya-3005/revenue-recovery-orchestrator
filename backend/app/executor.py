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

        # Internal actions — no external API call, no customer contact
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

        if action == RecoveryActionType.RETRY_CHARGE:
            return self._retry_saved_method(case, decision)

        if action == RecoveryActionType.SEND_REMINDER:
            return self._send_reminder_only(case, decision)

        # CREATE_PAYMENT_LINK, OFFER_DISCOUNT, SWITCH_RAIL all put a real
        # payment link in front of the customer
        return self._create_payment_link(case, decision)

    def _retry_saved_method(self, case: RecoveryCase, decision: DecisionResult) -> ExecutionResult:
        """RETRY_CHARGE — silently re-attempt the customer's saved payment
        method. No customer contact, so this never reaches comms (see
        pipeline.CUSTOMER_FACING_ACTIONS).

        This schema doesn't store a saved payment-method token, so in dry-run
        we simulate the silent retry the same way a real saved-mandate retry
        would behave (no customer contact, outcome confirmed asynchronously).
        In real (non-dry-run) mode we don't have a token to charge, so we
        fail loudly and honestly instead of pretending to have retried —
        the decision engine sees the failure via retry_count and falls
        through to a customer-facing action (switch_rail / payment link) on
        the next attempt."""
        if not self.client:
            return ExecutionResult(
                status=ExecutionStatus.DRY_RUN, action_taken=RecoveryActionType.RETRY_CHARGE,
                reason="Dry run — simulated silent retry of the saved payment method (no customer contact).",
                action_parameters_used=dict(decision.action_parameters),
            )
        return ExecutionResult(
            status=ExecutionStatus.FAILED, action_taken=RecoveryActionType.RETRY_CHARGE,
            reason="No stored payment-method token on file — cannot silently retry. "
                   "Falling through to a customer-facing recovery action.",
        )

    def _send_reminder_only(self, case: RecoveryCase, decision: DecisionResult) -> ExecutionResult:
        if self.client:
            return ExecutionResult(
                status=ExecutionStatus.FAILED, action_taken=RecoveryActionType.SEND_REMINDER,
                reason="No standalone messaging integration (SMS/email provider) is wired up — "
                       "cannot send a link-free reminder. Falling through to a payment link.",
            )
        channel = decision.action_parameters.get("channel", "email")
        return ExecutionResult(
            status=ExecutionStatus.DRY_RUN, action_taken=RecoveryActionType.SEND_REMINDER,
            reason=f"Reminder queued via {channel} — no new payment link created.",
            action_parameters_used=dict(decision.action_parameters),
        )

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

        # Idempotency key based on case + execution retries + follow-up
        # passes, so a follow-up re-engagement (Feature 7) generates a fresh
        # link instead of Razorpay silently handing back the original one.
        ref_id = f"{case.id[:20]}_{case.retry_count}_{case.follow_up_count}"

        # Resolve the notification channel BEFORE branching on dry-run vs.
        # live, so dry-run results reflect the same behavior a live call
        # would have. Razorpay's payment_link.notify only supports
        # email/sms — there's no WhatsApp delivery integration in this
        # codebase. Rather than silently creating a link with no notify
        # method at all (which looked like success while nobody was
        # actually contacted), we downgrade to SMS and record the downgrade
        # so it's visible, not hidden — in dry-run and live alike.
        channel = params.get("channel", "email")
        channel_downgraded_from = None
        if channel == "whatsapp":
            logger.warning(
                f"Case {case.id}: WhatsApp requested but not integrated — downgrading to SMS."
            )
            channel_downgraded_from = "whatsapp"
            channel = "sms"

        # Dry-run mode
        if not self.client:
            result_params = dict(params)
            result_params["amount_charged_paise"] = amount
            result_params["channel"] = channel
            if channel_downgraded_from:
                result_params["channel_downgraded_from"] = channel_downgraded_from
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

            if channel == "sms":
                link_data["notify"] = {"sms": 1, "email": 0}
            else:
                link_data["notify"] = {"email": 1, "sms": 0}

            payment_link = self.client.payment_link.create(data=link_data)

            result_params = dict(params)
            result_params["amount_charged_paise"] = amount
            result_params["channel"] = channel
            if channel_downgraded_from:
                result_params["channel_downgraded_from"] = channel_downgraded_from
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
