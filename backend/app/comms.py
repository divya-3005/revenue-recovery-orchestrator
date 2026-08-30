"""
Customer Communication Generator (Feature 6).

Generates personalized recovery messages based on:
  - Diagnosis category (why the payment is at risk)
  - Attempt number (tone escalates: gentle → firm → final)
  - Channel (email gets detailed copy, SMS/WhatsApp gets concise copy)
"""

from app.models import RecoveryCase, DiagnosisResult, RootCauseCategory


def generate_message(
    case: RecoveryCase, diagnosis: DiagnosisResult, attempt: int, channel: str = "email"
) -> str:
    """
    Generate a customer-facing recovery message.

    Args:
        case: The recovery case
        diagnosis: AI diagnosis result
        attempt: Which attempt (1=gentle, 2=firm, 3+=final)
        channel: 'email', 'sms', or 'whatsapp'
    """
    amount_inr = case.amount_paise / 100
    reason = diagnosis.specific_reason.replace("_", " ")

    # Pick tone based on attempt number
    if attempt <= 1:
        tone = "gentle"
    elif attempt == 2:
        tone = "firm"
    else:
        tone = "final"

    compact = channel.lower() in ("sms", "whatsapp")
    cat = diagnosis.root_cause_category

    if compact:
        return _sms_template(cat, tone, amount_inr, reason)
    return _email_template(cat, tone, amount_inr, reason)


def _sms_template(cat: RootCauseCategory, tone: str, amount: float, reason: str) -> str:
    """Short templates for SMS/WhatsApp (<160 chars)."""
    templates = {
        RootCauseCategory.SOFT_DECLINE: {
            "gentle": f"Payment of ₹{amount:,.0f} failed ({reason}). Tap link to retry securely.",
            "firm": f"Reminder: ₹{amount:,.0f} pending due to {reason}. Pay now to avoid disruption.",
            "final": f"Final Notice: ₹{amount:,.0f} unpaid ({reason}). Pay immediately to restore service.",
        },
        RootCauseCategory.FRICTION: {
            "gentle": f"Your cart (₹{amount:,.0f}) is saved! Complete checkout in one click.",
            "firm": f"Items in your ₹{amount:,.0f} order are waiting. Finish before stock expires.",
            "final": f"Last chance: Your ₹{amount:,.0f} cart expires today. Complete your order now.",
        },
        RootCauseCategory.MISSED_PAYMENT: {
            "gentle": f"Reminder: Invoice of ₹{amount:,.0f} is due. Please settle at your convenience.",
            "firm": f"Overdue: ₹{amount:,.0f} invoice is past due. Pay now to avoid late fees.",
            "final": f"Urgent: ₹{amount:,.0f} invoice significantly overdue. Settle immediately.",
        },
        RootCauseCategory.HARD_DECLINE: {
            "gentle": f"Issue with ₹{amount:,.0f} transaction. Our support team will reach out.",
            "firm": f"Update needed on ₹{amount:,.0f} payment. Please contact support.",
            "final": f"Important: Contact support regarding unresolved ₹{amount:,.0f} payment.",
        },
        RootCauseCategory.DISPUTE: {
            "gentle": f"We noted your concern on ₹{amount:,.0f} payment. We're reviewing.",
            "firm": f"Follow-up: Info needed for ₹{amount:,.0f} dispute. Reply to assist.",
            "final": f"Urgent: Please respond regarding your ₹{amount:,.0f} transaction review.",
        },
    }
    default = f"Update regarding your ₹{amount:,.0f} payment. Tap to review."
    return templates.get(cat, {}).get(tone, default)


def _email_template(cat: RootCauseCategory, tone: str, amount: float, reason: str) -> str:
    """Detailed email templates with context and resolution instructions."""
    templates = {
        RootCauseCategory.SOFT_DECLINE: {
            "gentle": f"Dear Customer,\n\nWe encountered an issue processing your payment of ₹{amount:,.0f} ({reason}). No charges were made. Please retry using the secure payment link below.",
            "firm": f"Dear Customer,\n\nFollow-up: your ₹{amount:,.0f} payment is still outstanding ({reason}). Please complete payment to maintain service access.",
            "final": f"Dear Customer,\n\nFinal notice: ₹{amount:,.0f} remains unpaid ({reason}). Please settle immediately to avoid service cancellation.",
        },
        RootCauseCategory.FRICTION: {
            "gentle": f"Hi,\n\nYou didn't finish your ₹{amount:,.0f} checkout. Your items are saved — pick up where you left off using the link below.",
            "firm": f"Hi,\n\nYour cart (₹{amount:,.0f}) is still reserved. Complete checkout soon before inventory is released.",
            "final": f"Hi,\n\nFinal reminder: your reserved cart (₹{amount:,.0f}) expires shortly. Complete your order now.",
        },
        RootCauseCategory.MISSED_PAYMENT: {
            "gentle": f"Dear Customer,\n\nFriendly reminder: your invoice of ₹{amount:,.0f} is now due. Pay conveniently online.",
            "firm": f"Dear Customer,\n\nYour ₹{amount:,.0f} invoice is overdue. Please pay promptly to avoid late charges.",
            "final": f"Dear Customer,\n\nUrgent: ₹{amount:,.0f} invoice is significantly overdue. Immediate settlement required.",
        },
        RootCauseCategory.HARD_DECLINE: {
            "gentle": f"Dear Customer,\n\nWe were unable to process your ₹{amount:,.0f} payment. Our team is reviewing and will follow up.",
            "firm": f"Dear Customer,\n\nFollow-up on the ₹{amount:,.0f} transaction decline. Please contact support or update billing.",
            "final": f"Dear Customer,\n\nYour ₹{amount:,.0f} payment requires immediate attention. Please contact support.",
        },
        RootCauseCategory.DISPUTE: {
            "gentle": f"Dear Customer,\n\nWe received notice regarding your ₹{amount:,.0f} transaction. Our team has initiated a review.",
            "firm": f"Dear Customer,\n\nWe're investigating the dispute on your ₹{amount:,.0f} transaction. Please reply with supporting documentation.",
            "final": f"Dear Customer,\n\nWe need your response regarding the ₹{amount:,.0f} transaction review for timely resolution.",
        },
    }
    default = f"Dear Customer,\n\nRegarding your ₹{amount:,.0f} payment — please review the payment link to proceed."
    return templates.get(cat, {}).get(tone, default)
