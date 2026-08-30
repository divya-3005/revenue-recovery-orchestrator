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
    Generate a customer-facing recovery message tailored to the communication channel and diagnosis.

    Args:
        case: The recovery case context
        diagnosis: The AI-produced diagnosis
        attempt: Which attempt this is (1-indexed). Controls tone:
            1 → gentle, 2 → firm, 3+ → final
        channel: Delivery channel ("sms", "whatsapp", or "email")
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

    is_compact_channel = channel.lower() in ["sms", "whatsapp"]

    if is_compact_channel:
        # SMS & WhatsApp templates: Concisely constrained to <160 characters
        templates = {
            RootCauseCategory.SOFT_DECLINE: {
                "gentle": f"Payment of ₹{amount_inr:,.0f} failed ({reason}). Tap link to retry payment securely.",
                "firm": f"Reminder: ₹{amount_inr:,.0f} pending due to {reason}. Settle now to avoid service disruption.",
                "final": f"Final Notice: ₹{amount_inr:,.0f} unpaid ({reason}). Pay immediately to restore service.",
            },
            RootCauseCategory.FRICTION: {
                "gentle": f"Your cart (₹{amount_inr:,.0f}) is saved! Complete your checkout in one click here.",
                "firm": f"Items in your ₹{amount_inr:,.0f} order are waiting. Finish checkout before stock expires.",
                "final": f"Last chance: Your ₹{amount_inr:,.0f} cart is expiring today. Complete your order now.",
            },
            RootCauseCategory.MISSED_PAYMENT: {
                "gentle": f"Reminder: Invoice of ₹{amount_inr:,.0f} is due. Please settle at your convenience.",
                "firm": f"Overdue notice: ₹{amount_inr:,.0f} invoice is past due. Pay now to avoid late fees.",
                "final": f"Urgent: ₹{amount_inr:,.0f} invoice significantly overdue. Settle immediately.",
            },
            RootCauseCategory.HARD_DECLINE: {
                "gentle": f"Issue with ₹{amount_inr:,.0f} transaction. Our support team will reach out shortly.",
                "firm": f"Update needed on ₹{amount_inr:,.0f} payment. Please contact support to resolve.",
                "final": f"Important: Contact support regarding unresolved ₹{amount_inr:,.0f} payment.",
            },
            RootCauseCategory.DISPUTE: {
                "gentle": f"We noted your concern on ₹{amount_inr:,.0f} payment. We are reviewing the details.",
                "firm": f"Follow-up: Additional info needed for ₹{amount_inr:,.0f} dispute. Reply to assist.",
                "final": f"Urgent: Please respond regarding your ₹{amount_inr:,.0f} transaction review.",
            },
        }
        default = {
            "gentle": f"Update regarding your ₹{amount_inr:,.0f} payment. Tap to review details.",
            "firm": f"Reminder: ₹{amount_inr:,.0f} payment pending. Please resolve promptly.",
            "final": f"Immediate action required for your ₹{amount_inr:,.0f} payment.",
        }
    else:
        # Email templates: Detailed context, explanation, and clear resolution instructions
        templates = {
            RootCauseCategory.SOFT_DECLINE: {
                "gentle": f"Dear Customer,\n\nWe encountered an issue processing your recent payment of ₹{amount_inr:,.0f} ({reason}). No charges were made. Please review your payment details and retry using the secure payment link below.",
                "firm": f"Dear Customer,\n\nThis is a follow-up reminder that your ₹{amount_inr:,.0f} payment is still outstanding due to {reason}. To maintain uninterrupted access to your services, please complete the payment at your earliest convenience.",
                "final": f"Dear Customer,\n\nFinal notice regarding your outstanding balance of ₹{amount_inr:,.0f} ({reason}). Please settle this payment immediately using the attached link to avoid service cancellation.",
            },
            RootCauseCategory.FRICTION: {
                "gentle": f"Hi there,\n\nWe noticed you didn't finish completing your ₹{amount_inr:,.0f} checkout. Your selected items are saved in your cart. You can pick up right where you left off using the link below.",
                "firm": f"Hi there,\n\nYour cart with items totaling ₹{amount_inr:,.0f} is still reserved for you. Please complete your checkout soon before inventory is released.",
                "final": f"Hi there,\n\nThis is a final reminder that your reserved cart (₹{amount_inr:,.0f}) will expire shortly. Complete your order now to secure your items.",
            },
            RootCauseCategory.MISSED_PAYMENT: {
                "gentle": f"Dear Customer,\n\nThis is a friendly reminder that your invoice totaling ₹{amount_inr:,.0f} is now due. You can view the invoice breakdown and make payment directly online.",
                "firm": f"Dear Customer,\n\nYour invoice for ₹{amount_inr:,.0f} is currently overdue. Please submit payment promptly to maintain your account in good standing and prevent late charges.",
                "final": f"Dear Customer,\n\nUrgent notice: Your invoice of ₹{amount_inr:,.0f} is significantly overdue. Immediate settlement is required to prevent account suspension.",
            },
            RootCauseCategory.HARD_DECLINE: {
                "gentle": f"Dear Customer,\n\nWe were unable to process your payment of ₹{amount_inr:,.0f}. Our account support team is reviewing the issue and will follow up with recommended next steps.",
                "firm": f"Dear Customer,\n\nWe are following up regarding the transaction decline for ₹{amount_inr:,.0f}. Please contact our support team or update your billing method.",
                "final": f"Dear Customer,\n\nYour payment of ₹{amount_inr:,.0f} could not be completed and requires your immediate attention. Please contact support to resolve.",
            },
            RootCauseCategory.DISPUTE: {
                "gentle": f"Dear Customer,\n\nWe have received notice of a concern regarding your transaction of ₹{amount_inr:,.0f}. Our team has initiated a review and will share updates shortly.",
                "firm": f"Dear Customer,\n\nWe are actively investigating the dispute on your ₹{amount_inr:,.0f} transaction. Please reply with any supporting documentation to assist our review.",
                "final": f"Dear Customer,\n\nWe require your response regarding the pending review of transaction ₹{amount_inr:,.0f} to reach a timely resolution.",
            },
        }
        default = {
            "gentle": f"Dear Customer,\n\nWe are contacting you regarding your payment of ₹{amount_inr:,.0f}. Please review the payment link to proceed.",
            "firm": f"Dear Customer,\n\nReminder regarding your pending payment of ₹{amount_inr:,.0f}. Please resolve this at your earliest convenience.",
            "final": f"Dear Customer,\n\nFinal notice regarding your pending payment of ₹{amount_inr:,.0f}. Immediate action is required.",
        }

    category_templates = templates.get(diagnosis.root_cause_category, default)
    return category_templates.get(tone, default["gentle"])
