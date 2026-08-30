import hashlib
from abc import ABC, abstractmethod
from typing import Any
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
        if not isinstance(approved_decision, PolicyApprovedDecision):  # type: ignore[unnecessary-isinstance]
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
    Real executor that wraps the razorpay-python SDK to execute recovery actions.

    Supported actions:
      - RETRY_CHARGE → creates a Payment Link
      - SEND_REMINDER → creates a Payment Link with reminder messaging
      - OFFER_DISCOUNT → creates a Payment Link with a discounted amount
      - ESCALATE_TO_HUMAN → internal action, no API call
      - STOP → internal action, no API call
    """

    def __init__(self):
        import importlib
        import os

        key_id = os.getenv("RAZORPAY_KEY_ID", "dummy_key_id")
        key_secret = os.getenv("RAZORPAY_KEY_SECRET", "dummy_key_secret")

        try:
            # Import lazily so the app remains usable in local/test environments without the SDK installed.
            razorpay_module = importlib.import_module("razorpay")
            self.client: Any = razorpay_module.Client(auth=(key_id, key_secret))
        except Exception:
            # Keep the executor importable in test and local environments where the SDK is not installed.
            self.client = None

    def execute(self, case: RecoveryCaseContext, approved_decision: PolicyApprovedDecision) -> ExecutionResult:
        if not isinstance(approved_decision, PolicyApprovedDecision):  # type: ignore[unnecessary-isinstance]
            raise TypeError("RazorpayExecutor requires a PolicyApprovedDecision. Unapproved decisions cannot be executed.")

        action = approved_decision.decision.recommended_action

        decision_id = approved_decision.decision.decision_id
        decision_hash = approved_decision.decision.decision_hash or approved_decision.decision.canonical_hash()

        # Human Approval Gate Enforcement: exact approved decision identity must match.
        from app.models import ApprovalStatus
        if approved_decision.requires_human_approval or case.approval_status == ApprovalStatus.APPROVED.value:
            if case.approval_status != ApprovalStatus.APPROVED.value:
                return ExecutionResult(
                    status=ExecutionStatus.FAILED,
                    action_taken=action,
                    reason=f"Execution blocked: Requires human approval which has not been granted (status: {case.approval_status}).",
                    action_parameters_used=approved_decision.decision.action_parameters
                )

            if not case.approved_decision_id or not case.approved_decision_hash:
                return ExecutionResult(
                    status=ExecutionStatus.FAILED,
                    action_taken=action,
                    reason="Execution blocked: Approved decision identity is missing.",
                    action_parameters_used=approved_decision.decision.action_parameters
                )

            if case.approved_decision_id != decision_id:
                return ExecutionResult(
                    status=ExecutionStatus.FAILED,
                    action_taken=action,
                    reason="Execution blocked: Approved decision ID mismatch.",
                    action_parameters_used=approved_decision.decision.action_parameters
                )

            if case.approved_decision_hash != decision_hash:
                return ExecutionResult(
                    status=ExecutionStatus.FAILED,
                    action_taken=action,
                    reason="Execution blocked: Approved decision hash mismatch. Stale approval.",
                    action_parameters_used=approved_decision.decision.action_parameters
                )

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

        if action == RecoveryActionType.RETRY_CHARGE:
            return ExecutionResult(
                status=ExecutionStatus.UNSUPPORTED,
                action_taken=action,
                reason="Native test-mode Razorpay retry API is not supported in this MVP. Use create_payment_link instead.",
                action_parameters_used=approved_decision.decision.action_parameters
            )

        # ── Payment Link actions ─────────
        if action in [RecoveryActionType.CREATE_PAYMENT_LINK, RecoveryActionType.SEND_REMINDER]:
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

    def _create_payment_link(self, case: RecoveryCaseContext, approved_decision: PolicyApprovedDecision, amount_override: int | None = None) -> ExecutionResult:
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

            def _extract_customer_info():
                payload = case.raw_signal_payload or {}
                # Try razorpay webhook format
                payment = payload.get("payload", {}).get("payment", {}).get("entity", {})
                email = payment.get("email") or payload.get("email")
                contact = payment.get("contact") or payload.get("contact")
                
                contact_str = str(contact) if contact else None
                email_str = str(email) if email else None
                
                if not contact_str and not email_str:
                    raise ValueError("Customer contact information unavailable")
                
                return {
                    "contact": contact_str,
                    "email": email_str
                }
            
            try:
                customer_info = _extract_customer_info()
            except ValueError as e:
                return ExecutionResult(
                    status=ExecutionStatus.FAILED,
                    action_taken=action,
                    reason=f"CUSTOMER_CONTACT_MISSING: {str(e)}",
                    action_parameters_used=approved_decision.decision.action_parameters
                )

            link_data: dict[str, Any] = {
                "amount": amount_to_charge,
                "currency": case.currency,
                "description": description,
                "reference_id": reference_id,
                "customer": customer_info,
                "notes": {
                    "case_id": case.id,
                    "action": action.value
                }
            }

            # Set Razorpay notify field for actual channel dispatch
            channel = approved_decision.decision.action_parameters.get("channel", "email")
            if action == RecoveryActionType.SEND_REMINDER:
                if channel == "sms":
                    link_data["notify"] = {"sms": 1, "email": 0}
                elif channel == "whatsapp":
                    # Razorpay Payment Links API does not support native WhatsApp notify.
                    # We simulate it here, skipping the 'notify' dictionary.
                    pass
                else:  # email (default)
                    link_data["notify"] = {"email": 1, "sms": 0}
            elif action == RecoveryActionType.OFFER_DISCOUNT:
                # Discounts always notified via email so customer sees the offer details
                link_data["notify"] = {"email": 1, "sms": 0}

            # SDK call: payment_link.create(data) — no headers kwarg
            payment_client = self.client
            if payment_client is None:
                return ExecutionResult(
                    status=ExecutionStatus.FAILED,
                    action_taken=action,
                    reason="Razorpay SDK is unavailable in this environment; cannot create payment link.",
                    external_reference_id=None,
                    action_parameters_used=approved_decision.decision.action_parameters.copy()
                )

            payment_link: Any = payment_client.payment_link.create(data=link_data)

            # Important: make a copy of parameters to avoid mutating the original decision
            params_used = approved_decision.decision.action_parameters.copy()
            params_used["amount_charged_paise"] = amount_to_charge
            
            if action == RecoveryActionType.SEND_REMINDER and channel == "whatsapp":
                params_used["simulated_whatsapp"] = True
                params_used["simulation_reason"] = "WhatsApp is simulated — link generated, dispatch would go through WhatsApp Business API in production."
            
            return ExecutionResult(
                status=ExecutionStatus.SUCCESS,
                action_taken=action,
                reason="Razorpay payment link created successfully.",
                external_reference_id=payment_link.get("id"),
                action_parameters_used=params_used
            )
        except Exception as e:
            error_msg = str(e).lower()
            if "reference_id" in error_msg and "already exists" in error_msg:
                # Idempotency hit: the link was already created in a previous (crashed) attempt.
                return ExecutionResult(
                    status=ExecutionStatus.SUCCESS,
                    action_taken=action,
                    reason="Razorpay payment link already exists (idempotency recovered).",
                    external_reference_id="idempotent_recovery",
                    action_parameters_used=approved_decision.decision.action_parameters.copy()
                )

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
