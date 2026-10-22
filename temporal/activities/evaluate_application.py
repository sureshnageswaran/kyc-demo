"""Activity 5: app_evaluate_application."""
from temporalio import activity

from .. import kyc_logic
from ..models import EvaluateInput, EvaluationResult
from .base import ActivityBase


class EvaluateApplication(ActivityBase):
    @activity.defn(name="app_evaluate_application")
    async def app_evaluate_application(self, inp: EvaluateInput) -> EvaluationResult:
        # Pure: no Garage reads, service calls, or database writes.
        return kyc_logic.evaluate(inp)
