"""
Communication Generator — Feature 6

Generates personalized customer-facing recovery messages based on:
  - Diagnosis category (why the payment is at risk)
  - Attempt number (tone escalates: gentle → firm → final)

This is template-based for the MVP. No actual SMS/email sending.
"""

from app.domain import RecoveryCaseContext, DiagnosisResult
from app.domain import RootCauseCategory

def generate_message(case: RecoveryCaseContext, diagnosis: DiagnosisResult, attempt: int, channel: str = "email") -> str:
    """
    Generate a customer-facing recovery message.

    Args:
        case: The recovery case context
        diagnosis: The AI-produced diagnosis
        attempt: Which attempt this is (1-indexed). Controls tone:
            1 → gentle, 2 → firm, 3+ → final
    """
    amount_inr = case.amount_paise / 100
    reason = diagnosis.specific_reason.replace("_", " ")

    if channel in ["sms", "whatsapp"]:
        length_instruction = "Keep it extremely short and concise, suitable for an SMS or WhatsApp message (under 160 characters)."
    else:
        length_instruction = "Keep it concise and professional, suitable for an email."

    # Pick tone based on attempt number
    if attempt <= 1:
        tone = "gentle"
    elif attempt == 2:
        tone = "firm"
    else:
        tone = "final"

    # Category-specific message templates
    templates = {
        RootCauseCategory.SOFT_DECLINE: {
            "gentle": f"Hi, your payment of ₹{amount_inr:,.0f} could not be processed ({reason}). Please retry when ready.",
            "firm": f"Reminder: Your ₹{amount_inr:,.0f} payment is still pending due to {reason}. Please pay soon to avoid interruption.",
            "final": f"Final notice: ₹{amount_inr:,.0f} remains unpaid ({reason}). Immediate action required.",
        },
        RootCauseCategory.FRICTION: {
            "gentle": f"Hi, you didn't complete your ₹{amount_inr:,.0f} checkout. Your cart is saved — finish when ready.",
            "firm": f"Your ₹{amount_inr:,.0f} order is still waiting. Complete your purchase before it expires.",
            "final": f"Last chance: Your ₹{amount_inr:,.0f} cart expires soon. Complete checkout now.",
        },
        RootCauseCategory.MISSED_PAYMENT: {
            "gentle": f"Friendly reminder: Your ₹{amount_inr:,.0f} invoice is past due. Please pay at your convenience.",
            "firm": f"Your ₹{amount_inr:,.0f} invoice is overdue. Please settle promptly to avoid late fees.",
            "final": f"Final reminder: ₹{amount_inr:,.0f} invoice is significantly overdue. Immediate payment required.",
        },
        RootCauseCategory.HARD_DECLINE: {
            "gentle": f"We're reaching out about your ₹{amount_inr:,.0f} payment. Our team will follow up.",
            "firm": f"Regarding your ₹{amount_inr:,.0f} payment — please contact support for assistance.",
            "final": f"Your ₹{amount_inr:,.0f} payment requires attention. A representative will contact you.",
        },
        RootCauseCategory.DISPUTE: {
            "gentle": f"We received a concern regarding your ₹{amount_inr:,.0f} transaction. We're reviewing it.",
            "firm": f"Follow-up on your ₹{amount_inr:,.0f} dispute. Please share any additional details.",
            "final": f"Your ₹{amount_inr:,.0f} dispute needs your response to avoid further escalation.",
        },
    }

    # Fallback for UNKNOWN or any new category
    default = {
        "gentle": f"Hi, we're reaching out about your ₹{amount_inr:,.0f} payment.",
        "firm": f"Reminder about your ₹{amount_inr:,.0f} payment. Please contact us.",
        "final": f"Your ₹{amount_inr:,.0f} payment requires immediate attention.",
    }

    category_templates = templates.get(diagnosis.root_cause_category, default)
    return category_templates.get(tone, default["gentle"])
