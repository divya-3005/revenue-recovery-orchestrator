import json
from app.domain import *
case = RecoveryCaseContext(
    id="123", case_type=CaseType.CHECKOUT_ABANDONED, status=CaseStatus.OPEN,
    amount_paise=100, currency="INR", customer_id="c1", retry_count=0, cumulative_discount_paise=0, raw_signal_payload={}
)
try:
    json.dumps(case.model_dump())
    print("case ok")
except Exception as e: print("case error:", e)

diag = DiagnosisResult(root_cause_category=RootCauseCategory.HARD_DECLINE, specific_reason="a", confidence_score=0.9, reasoning="b")
try:
    json.dumps(diag.model_dump())
    print("diag ok")
except Exception as e: print("diag error:", e)

dec = DecisionResult(recommended_action=RecoveryActionType.RETRY_CHARGE, action_parameters={"delay_hours": 1}, confidence_score=0.9, reasoning="b")
try:
    json.dumps(dec.model_dump())
    print("dec ok")
except Exception as e: print("dec error:", e)

pol = PolicyEvaluationResult(allowed=True, reason="ok")
try:
    json.dumps(pol.model_dump())
    print("pol ok")
except Exception as e: print("pol error:", e)
