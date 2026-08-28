import hashlib
from abc import ABC, abstractmethod
from app.domain import (
    RecoveryCaseContext, PolicyApprovedDecision,
    ExecutionResult, ExecutionStatus, RecoveryActionType
)


class ActionExecutor(ABC):
    """Base class for all executors. Requires a PolicyApprovedDecision — never a raw AI decision."""

    @abstractmethod
    def execute(self, case: RecoveryCaseContext, approved_decision: PolicyApprovedDecision) -> ExecutionResult:
        pass


class DryRunExecutor(ActionExecutor):
    """Safe executor for testing — logs what would happen without calling any external API."""

    def execute(self, case: RecoveryCaseContext, approved_decision: PolicyApprovedDecision) -> ExecutionResult:
        # Runtime enforcement: only PolicyApprovedDecision can reach here
        if not isinstance(approved_decision, PolicyApprovedDecision):
            raise TypeError("ActionExecutor requires a PolicyApprovedDecision. Unapproved decisions cannot be executed.")

        return ExecutionResult(
            status=ExecutionStatus.DRY_RUN,
            action_taken=approved_decision.decision.recommended_action,
            reason="Dry run: action was not executed externally",
            external_reference_id=None,
            action_parameters_used=approved_decision.decision.action_parameters
        )


class RazorpayExecutor(ActionExecutor):
    """
    Real executor that calls Razorpay's test-mode APIs.

    Supported actions:
      - RETRY_CHARGE → creates a Payment Link (customer gets a new payment URL)
      - SEND_REMINDER → creates a Payment Link with reminder messaging
      - ESCALATE_TO_HUMAN → internal action, no API call
      - STOP → internal action, no API call
      - OFFER_DISCOUNT → not supported in MVP
    """

    def __init__(self):
        import os
        import razorpay

        key_id = os.getenv("RAZORPAY_KEY_ID", "dummy_key_id")
        key_secret = os.getenv("RAZORPAY_KEY_SECRET", "dummy_key_secret")

        self.client = razorpay.Client(auth=(key_id, key_secret))

    def execute(self, case: RecoveryCaseContext, approved_decision: PolicyApprovedDecision) -> ExecutionResult:
        # Runtime enforcement: only PolicyApprovedDecision can reach here
        if not isinstance(approved_decision, PolicyApprovedDecision):
            raise TypeError("RazorpayExecutor requires a PolicyApprovedDecision. Unapproved decisions cannot be executed.")

        action = approved_decision.decision.recommended_action

        # ── Internal actions (no external API call) ──────────────────────
        if action == RecoveryActionType.ESCALATE_TO_HUMAN:
            return ExecutionResult(
                status=ExecutionStatus.SUCCESS,
                action_taken=action,
                reason="Case escalated to human review queue.",
                action_parameters_used=approved_decision.decision.action_parameters
            )

        if action == RecoveryActionType.STOP:
            return ExecutionResult(
                status=ExecutionStatus.SUCCESS,
                action_taken=action,
                reason="Case closed — recovery stopped.",
                action_parameters_used=approved_decision.decision.action_parameters
            )

        # ── Payment Link actions (RETRY_CHARGE / SEND_REMINDER / OFFER_DISCOUNT) ─────────
        if action in [RecoveryActionType.RETRY_CHARGE, RecoveryActionType.SEND_REMINDER]:
            return self._create_payment_link(case, approved_decision)
            
        if action == RecoveryActionType.OFFER_DISCOUNT:
            discount_percent = approved_decision.decision.action_parameters.get("discount_percent", 0)
            discount_amount_paise = int(case.amount_paise * (discount_percent / 100.0))
            discounted_amount = case.amount_paise - discount_amount_paise
            
            result = self._create_payment_link(case, approved_decision, amount_override=discounted_amount)
            # Record the actual discount applied so it can be persisted by the caller
            result.action_parameters_used["discount_applied_paise"] = discount_amount_paise
            return result

        # ── Unsupported actions ────────────────────
        return ExecutionResult(
            status=ExecutionStatus.FAILED,
            action_taken=action,
            reason=f"Action {action.value} is not supported by RazorpayExecutor MVP.",
            action_parameters_used=approved_decision.decision.action_parameters
        )

    def _create_payment_link(self, case: RecoveryCaseContext, approved_decision: PolicyApprovedDecision, amount_override: int = None) -> ExecutionResult:
        """Create a Razorpay Payment Link for retry or reminder actions."""
        action = approved_decision.decision.recommended_action

        try:
            # Deterministic reference_id for idempotency.
            # Razorpay enforces uniqueness on reference_id — returns 400 on duplicate.
            # Max 40 characters allowed by Razorpay API.
            reference_id = self._make_reference_id(case, action.value)

            if action == RecoveryActionType.OFFER_DISCOUNT:
                description = f"Discounted payment for case {case.id}"
            else:
                description = (
                    f"Retry payment for case {case.id}"
                    if action == RecoveryActionType.RETRY_CHARGE
                    else f"Payment reminder for case {case.id}"
                )
                
            amount_to_charge = amount_override if amount_override is not None else case.amount_paise

            link_data = {
                "amount": amount_to_charge,
                "currency": case.currency,
                "description": description,
                "reference_id": reference_id,
                "customer": {
                    "contact": "9999999999",   # placeholder for MVP
                    "email": "customer@example.com"
                },
                "notes": {
                    "case_id": case.id,
                    "action": action.value
                }
            }

            # SDK call: payment_link.create(data) — no headers kwarg
            payment_link = self.client.payment_link.create(data=link_data)

            # Important: make a copy of parameters to avoid mutating the original decision
            params_used = approved_decision.decision.action_parameters.copy()
            params_used["amount_charged_paise"] = amount_to_charge
            
            return ExecutionResult(
                status=ExecutionStatus.SUCCESS,
                action_taken=action,
                reason="Razorpay payment link created successfully.",
                external_reference_id=payment_link.get("id"),
                action_parameters_used=params_used
            )
        except Exception as e:
            return ExecutionResult(
                status=ExecutionStatus.FAILED,
                action_taken=action,
                reason=f"Razorpay API failure: {str(e)}",
                external_reference_id=None,
                action_parameters_used=approved_decision.decision.action_parameters.copy()
            )

    @staticmethod
    def _make_reference_id(case: RecoveryCaseContext, action_label: str) -> str:
        """Generate a deterministic reference_id ≤ 40 chars for Razorpay idempotency."""
        raw = f"{case.id}-{action_label}-{case.retry_count}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]
