import enum
from typing import Tuple, Dict, Any

class RecoveryActionType(str, enum.Enum):
    RETRY_CHARGE = "retry_charge"
    SEND_REMINDER = "send_reminder"
    OFFER_DISCOUNT = "offer_discount"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    STOP = "stop"

class PolicyConfig:
    MAX_RETRIES: int = 3
    MAX_DISCOUNT_PERCENT: int = 15
    # Value above which automatic financial actions are blocked (50,000 INR = 5000000 paise)
    REQUIRE_HUMAN_APPROVAL_ABOVE_PAISE: int = 5000000 
    BLOCK_HARD_DECLINES: bool = True

def evaluate_policy(case, proposed_action: RecoveryActionType, proposed_parameters: Dict[str, Any] = None) -> Tuple[bool, str]:
    """
    Deterministic rule layer that gates the AI decision engine.
    Returns (is_allowed, reason).
    """
    if proposed_parameters is None:
        proposed_parameters = {}

    # Rule 1: Escalation and stopping are always allowed
    if proposed_action in [RecoveryActionType.ESCALATE_TO_HUMAN, RecoveryActionType.STOP]:
        return True, f"Action {proposed_action.value} is always permitted."

    # Rule 2: High value cases require human approval for financial actions
    if case.amount_paise > PolicyConfig.REQUIRE_HUMAN_APPROVAL_ABOVE_PAISE:
        if proposed_action != RecoveryActionType.SEND_REMINDER: 
            return False, f"Action blocked: Case value ({case.amount_paise} paise) exceeds automatic threshold. Human approval required."

    # Rule 3: Cap discount percentage
    if proposed_action == RecoveryActionType.OFFER_DISCOUNT:
        discount_pct = proposed_parameters.get("discount_percent", 0)
        if discount_pct > PolicyConfig.MAX_DISCOUNT_PERCENT:
            return False, f"Action blocked: Proposed discount ({discount_pct}%) exceeds policy maximum ({PolicyConfig.MAX_DISCOUNT_PERCENT}%)."
        if discount_pct <= 0:
            return False, "Action blocked: Discount percentage must be greater than zero."

    # Rule 4: Block retries on hard declines
    if proposed_action == RecoveryActionType.RETRY_CHARGE and PolicyConfig.BLOCK_HARD_DECLINES:
        payload = case.raw_signal_payload or {}
        reason = payload.get("reason", "").lower()
        if "hard_decline" in reason or "lost_card" in reason or "stolen" in reason:
            return False, "Action blocked: Policy forbids retrying hard declines."

    # Rule 5: Max retries cap
    # FUTURE-DEPENDENT: Requires `retry_count` state to be persisted on RecoveryCase.
    if proposed_action == RecoveryActionType.RETRY_CHARGE:
        # TODO: Implement when retry_count is added
        # if getattr(case, 'retry_count', 0) >= PolicyConfig.MAX_RETRIES:
        #     return False, f"Action blocked: Max retries ({PolicyConfig.MAX_RETRIES}) reached."
        pass

    return True, "Action allowed by policy."
