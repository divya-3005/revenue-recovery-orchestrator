from abc import ABC, abstractmethod
from app.domain import (
    RecoveryCaseContext, PolicyApprovedDecision, 
    ExecutionResult, ExecutionStatus
)

class ActionExecutor(ABC):
    @abstractmethod
    def execute(self, case: RecoveryCaseContext, approved_decision: PolicyApprovedDecision) -> ExecutionResult:
        """Executes the approved action and returns the outcome."""
        pass

class DryRunExecutor(ActionExecutor):
    def execute(self, case: RecoveryCaseContext, approved_decision: PolicyApprovedDecision) -> ExecutionResult:
        # Runtime enforcement boundary
        if not isinstance(approved_decision, PolicyApprovedDecision):
            raise TypeError("ActionExecutor requires a PolicyApprovedDecision. Unapproved decisions cannot be executed.")
            
        return ExecutionResult(
            status=ExecutionStatus.DRY_RUN,
            action_taken=approved_decision.decision.recommended_action,
            reason="Dry run: action was not executed externally",
            external_reference_id=None,
            action_parameters_used=approved_decision.decision.action_parameters
        )
