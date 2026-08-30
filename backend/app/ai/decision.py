from app.domain import RecoveryCaseContext, DiagnosisResult, DecisionResult
from app.ai.provider import AIProvider
from app.policy import PolicyConfig

def decide_action(case: RecoveryCaseContext, diagnosis: DiagnosisResult, provider: AIProvider) -> DecisionResult:
    """
    Proposes a RecoveryActionType based on the diagnosis and case state.
    The output is deterministic-structurally validated but NOT policy-validated yet.
    """
    prompt = f"""
    You are an expert payments and revenue recovery AI. 
    Based on the following case history and root-cause diagnosis, propose the NEXT BEST action to recover revenue.
    
    [CASE STATE]
    Case ID: {case.id}
    Amount: {case.amount_paise} paise
    Retry Count: {case.retry_count} (Max allowed: {PolicyConfig.MAX_RETRIES})
    Cumulative Discount Given: {case.cumulative_discount_paise} paise
    
    [DIAGNOSIS]
    Category: {diagnosis.root_cause_category.value}
    Reason: {diagnosis.specific_reason}
    Confidence: {diagnosis.confidence_score}
    
    [INSTRUCTIONS]
    Propose the single best recovery action from the following list:
    1. retry_charge: Use if the diagnosis is a temporary 'soft_decline'. Provide parameter: {{"delay_hours": <int>}}. Do NOT use if max retries reached.
    2. offer_discount: Use for 'friction' or pricing-related drop-offs. Provide parameter: {{"discount_percent": <int>}}. Do NOT exceed {PolicyConfig.MAX_DISCOUNT_PERCENT}%.
    3. send_reminder: Use for 'missed_payment' or if the user simply needs a nudge. Provide parameter: {{"channel": "email" | "sms" | "whatsapp"}}.
    4. escalate_to_human: Use for 'dispute', 'hard_decline', high-value cases over {int(PolicyConfig.REQUIRE_HUMAN_APPROVAL_ABOVE_PAISE / 100):,} INR, or if the diagnosis is 'unknown'/'low confidence'.
    5. stop: Use if the case is unrecoverable and should be closed (e.g., fraud confirmed).
    
    You MUST output your decision with:
    - recommended_action (must exactly match one of the 5 options above)
    - action_parameters (as defined above)
    - confidence_score (0.0 to 1.0, based on how strongly you believe this action will succeed)
    - reasoning (1-2 sentences explaining why this action is optimal given the diagnosis and case state)
    """
    return provider.ask_structured(prompt, DecisionResult)
