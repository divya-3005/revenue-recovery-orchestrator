from app.domain import RecoveryCaseContext, DiagnosisResult
from app.ai.provider import AIProvider
import json

def diagnose_case(case: RecoveryCaseContext, provider: AIProvider) -> DiagnosisResult:
    """
    Asks the AI provider to diagnose the root cause of the revenue risk.
    Returns a strictly parsed DiagnosisResult.
    """
    history = case.customer_payment_history or (case.raw_signal_payload or {}).get("payment_history") or {}
    
    prompt = f"""
    You are an expert Razorpay payments and revenue recovery AI. 
    Analyze this revenue-at-risk case and diagnose the root cause.
    
    [CASE DATA]
    Case ID: {case.id}
    Type: {case.case_type.value}
    Amount: {case.amount_paise} paise
    Currency: {case.currency}
    Payment Rail: {case.payment_rail or "Not specified"}
    Customer Payment History: {json.dumps(history)}
    Raw Signal Data: {json.dumps(case.raw_signal_payload)}
    
    [INSTRUCTIONS]
    1. root_cause_category: Must strictly map to one of the following based on the Raw Signal Data and Payment History:
       - hard_decline: Permanent failure (e.g., stolen card, closed account, invalid mandate). Retries will definitively fail.
       - soft_decline: Temporary failure (e.g., insufficient funds, limit exceeded, network error). Retries might succeed.
       - friction: User abandoned a checkout or flow before completing (e.g., dropped off at OTP). When evaluating friction, analyze behavioral cues in Raw Signal Data: 'is_repeat_customer' (repeat visitors dropping off indicates checkout friction/distraction rather than disinterest), 'abandoned_hour' / time of day (late-night drop-offs often indicate interrupted sessions), and cart value.
       - dispute: Customer initiated a chargeback or flagged the transaction as fraud or contested invoice line items.
       - missed_payment: An invoice or payment link past due. In specific_reason and reasoning, distinguish between:
         * 'cash_flow_delay': Customer with strong historical on-time payment track record experiencing temporary cash-flow/liquidity delay.
         * 'simply_missed': Customer missed notifications/invoices without active dispute.
         * 'disputed_invoice': Customer has contested billing details.
       - unknown: The signal data is genuinely missing, completely ambiguous, or provides insufficient evidence to classify.
    
    2. specific_reason: A concise string summarizing the exact failure (e.g., 'insufficient_funds', 'cash_flow_delay', 'high_cart_value_drop_off', 'bank_network_timeout'). Do NOT invent error codes or facts not present in the data.
    
    3. confidence_score: A float from 0.0 to 1.0 indicating your confidence in this diagnosis based strictly on the quality of evidence.
       - 0.90 to 1.0: Clear, unambiguous signal data (e.g., explicit bank error codes or verified payment history).
       - 0.50 to 0.89: Educated guess based on partial behavioral data.
       - 0.0 to 0.49: Poor evidence quality. 
    
    4. reasoning: A 1-2 sentence plain-language explanation of how you reached this diagnosis. You MUST reference specific fields from the Raw Signal Data or Payment History to justify the conclusion. Do NOT hallucinate.
    """
    
    return provider.ask_structured(prompt, DiagnosisResult)
