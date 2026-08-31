"""
Synthetic case generation for batch processing (Feature 11).

Generates 50 cases spanning all three case types with realistic distributions.
"""

from app.models import CaseType


def compute_priority_score(case_type: str, amount_paise: int, payload: dict) -> int:
    """Expected Value heuristic: amount × estimated recovery probability."""
    if case_type == CaseType.SUBSCRIPTION_FAILED.value:
        hard_reasons = {"lost_card_reported", "stolen_card", "account_closed", "fraud_suspected"}
        if payload.get("reason") in hard_reasons:
            return int(amount_paise * 0.2)  # Low recovery probability
        return int(amount_paise * 0.8)  # High recovery probability
    if case_type == CaseType.CHECKOUT_ABANDONED.value:
        return int(amount_paise * 0.5)
    if case_type == CaseType.INVOICE_OVERDUE.value:
        # Recency-weighted: a 3-day-old invoice is far more collectable than
        # a 30-day-old one, so decay expected value as it ages.
        days = payload.get("days_overdue", 0)
        urgency = max(0.5, 1.0 - (days / 60))
        return int(amount_paise * 0.9 * urgency)
    return int(amount_paise * 0.5)


def generate_batch() -> list[dict]:
    """Generate 50 synthetic cases: 15 soft declines, 8 hard declines, 12 cart abandons, 10 invoices, 5 high-value."""
    cases = []

    # ── 15 subscription failures: soft declines (retryable) ──────────
    soft_reasons = ["insufficient_funds", "bank_network_timeout", "card_limit_exceeded",
                    "processing_error", "temporary_hold"]
    soft_amounts = [9900, 29900, 49900, 99900, 199900]
    soft_rails = ["card", "upi", "enach"]
    for i in range(15):
        amount = soft_amounts[i % len(soft_amounts)]
        cid = f"cust_batch_{i:03d}"
        payload = {"reason": soft_reasons[i % len(soft_reasons)],
                   "email": f"{cid}@example.com", "contact": f"+9198765{i:05d}"}
        cases.append(_case(CaseType.SUBSCRIPTION_FAILED, amount, cid, payload,
                          rail=soft_rails[i % len(soft_rails)]))

    # ── 8 subscription failures: hard declines (should NOT be retried) ──
    hard_reasons = ["lost_card_reported", "stolen_card", "account_closed", "fraud_suspected"]
    hard_amounts = [9900, 29900, 49900]
    for i in range(8):
        amount = hard_amounts[i % len(hard_amounts)]
        cid = f"cust_batch_{15 + i:03d}"
        payload = {"reason": hard_reasons[i % len(hard_reasons)],
                   "email": f"{cid}@example.com", "contact": f"+9198765{15 + i:05d}"}
        cases.append(_case(CaseType.SUBSCRIPTION_FAILED, amount, cid, payload, rail="card"))

    # ── 12 checkout abandoned: friction signals ──────────────────────
    cart_amounts = [19900, 49900, 99900, 249900, 499900]
    for i in range(12):
        amount = cart_amounts[i % len(cart_amounts)]
        cid = f"cust_batch_{23 + i:03d}"
        hour = (i * 7) % 24
        payload = {
            "cart_items": 1 + (i % 5),
            "is_repeat_customer": (i % 2 == 0),
            "abandoned_hour": hour,
            "time_of_day": "night" if hour in range(0, 6) or hour >= 22 else "day",
            "email": f"{cid}@example.com", "contact": f"+9198765{23 + i:05d}",
        }
        cases.append(_case(CaseType.CHECKOUT_ABANDONED, amount, cid, payload,
                          rail=["card", "upi"][i % 2]))

    # ── 10 invoice overdue: missed payments ─────────────────────────
    inv_amounts = [100000, 250000, 500000, 1000000, 2500000]
    for i in range(10):
        amount = inv_amounts[i % len(inv_amounts)]
        cid = f"cust_batch_{35 + i:03d}"
        good_history = (i % 2 == 0)
        payload = {
            "days_overdue": 3 + (i * 3) % 28,
            "invoice_number": f"INV-{1000 + i}",
            "payment_history": {
                "past_invoices_count": 8,
                "on_time_ratio": 0.88 if good_history else 0.40,
                "average_delay_days": 2 if good_history else 18,
            },
            "email": f"{cid}@example.com", "contact": f"+9198765{35 + i:05d}",
        }
        cases.append(_case(CaseType.INVOICE_OVERDUE, amount, cid, payload))

    # ── 5 high-value cases (trigger human approval) ─────────────────
    hv_amounts = [5500000, 7000000, 10000000]
    for i in range(5):
        amount = hv_amounts[i % len(hv_amounts)]
        cid = f"cust_batch_{45 + i:03d}"
        payload = {"reason": "insufficient_funds",
                   "email": f"{cid}@example.com", "contact": f"+9198765{45 + i:05d}"}
        cases.append(_case(CaseType.SUBSCRIPTION_FAILED, amount, cid, payload, rail="enach"))

    # Sort by priority (Feature 13: Recovery Prioritization)
    cases.sort(key=lambda c: c["priority_score"], reverse=True)
    return cases


def generate_demo_scenarios() -> list[dict]:
    """5 hand-crafted demo scenarios for the pitch video."""
    return [
        _case(CaseType.SUBSCRIPTION_FAILED, 499900, "cust_demo_01",
              {"reason": "insufficient_funds", "email": "rahul@example.com",
               "contact": "+919876500001"}, rail="card"),
        _case(CaseType.SUBSCRIPTION_FAILED, 299900, "cust_demo_02",
              {"reason": "lost_card_reported", "email": "priya@example.com",
               "contact": "+919876500002"}, rail="card"),
        _case(CaseType.CHECKOUT_ABANDONED, 299900, "cust_demo_03",
              {"cart_items": 2, "is_repeat_customer": True, "email": "amit@example.com",
               "contact": "+919876500003"}, rail="upi"),
        _case(CaseType.SUBSCRIPTION_FAILED, 7500000, "cust_demo_04",
              {"reason": "insufficient_funds", "email": "enterprise@example.com",
               "contact": "+919876500004"}, rail="enach"),
        _case(CaseType.INVOICE_OVERDUE, 1500000, "cust_demo_05",
              {"days_overdue": 7, "invoice_number": "INV-2026-005",
               "email": "vikram@example.com", "contact": "+919876500005"}, rail="upi"),
    ]


def _case(case_type: CaseType, amount: int, customer_id: str,
          payload: dict, rail: str | None = None) -> dict:
    """Build a synthetic case dict."""
    return {
        "case_type": case_type.value,
        "amount_paise": amount,
        "currency": "INR",
        "customer_id": customer_id,
        "customer_email": payload.get("email", f"{customer_id}@example.com"),
        "customer_phone": payload.get("contact"),
        "payment_rail": rail,
        "priority_score": compute_priority_score(case_type.value, amount, payload),
        "raw_signal_payload": payload,
    }
