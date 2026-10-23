"""Activity 5: app_evaluate_application (policy resolution + evaluation + disposition decision)."""
from temporalio import activity

from .. import kyc_logic
from ..models import EvaluateApplicationInput, EvaluateInput, EvaluationResult, LoadPolicyInput
from .base import ActivityBase


class EvaluateApplication(ActivityBase):
    @activity.defn(name="app_evaluate_application")
    async def app_evaluate_application(self, inp: EvaluateApplicationInput) -> EvaluationResult:
        """Load the policy as late as possible (right here), evaluate, and decide whether review is needed.

        Reads Garage for the policy only; no service calls or database writes. The returned result
        carries the policy snapshot, the screening findings, and the final review_required flag.
        """
        policy = await self.resolve_policy(LoadPolicyInput(
            policy_id=inp.policy_id, mode=inp.policy_mode, version=inp.policy_version, sha256=inp.policy_sha256))
        evaluation = kyc_logic.evaluate(EvaluateInput(
            evaluation_id=inp.evaluation_id, evaluation_sequence=inp.evaluation_sequence,
            declared_applicant=inp.declared_applicant, documents=inp.documents, policy=policy,
            evaluated_at=inp.evaluated_at))
        evaluation.policy = policy
        return kyc_logic.apply_screening(evaluation, inp.screening)
