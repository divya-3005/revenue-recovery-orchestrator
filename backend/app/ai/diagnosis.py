from app.domain import RecoveryCaseContext, DiagnosisResult
from app.ai.provider import AIProvider
import json

def diagnose_case(case: RecoveryCaseContext, provider: AIProvider) -> DiagnosisResult:
    """
    Asks the AI provider to diagnose the root cause of the revenue risk.
    Returns a strictly parsed DiagnosisResult.
    """
    
    prompt = f"""
    You are an expert payments and revenue recovery AI.
    Analyze the following case and provide a root cause diagnosis.
    
    Case ID: {case.id}
    Type: {case.case_type.value}
    Amount (paise): {case.amount_paise}
    Currency: {case.currency}
    Signal Data: {json.dumps(case.raw_signal_payload)}
    
    Based on the signal data, determine:
    1. The root_cause_category (must be one of: hard_decline, soft_decline, friction, dispute, missed_payment, unknown).
    2. The specific_reason (e.g., 'insufficient_funds', 'high_cart_value').
    3. Your confidence_score (between 0.0 and 1.0).
    4. A plain-language reasoning explaining how you reached this conclusion.
    """
    
    return provider.ask_structured(prompt, DiagnosisResult)
