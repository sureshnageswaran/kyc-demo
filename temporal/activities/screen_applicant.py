"""Activity 6: app_screen_applicant."""
from temporalio import activity

from .. import errors
from ..models import ScreenInput, ScreeningResult
from .base import ActivityBase, utcnow  # STUB-TODO(SCREENING): re-add json_or_none


class ScreenApplicant(ActivityBase):
    @activity.defn(name="app_screen_applicant")
    async def app_screen_applicant(self, inp: ScreenInput) -> ScreeningResult:
        attempt = activity.info().attempt
        # STUB-TODO(SCREENING): real screening call disabled until the screening stub exists.
        # Restore it (and the response validation) and delete the simulated body below.
        # resp = await self._post(
        #     f"{self.cfg.screening_url}/v1/screen", self.cfg.screening_http_timeout, self.cfg.screening_api_key,
        #     "screening",
        #     json={"application_id": inp.application_id, "client_reference": inp.client_reference,
        #           "applicant": inp.declared_applicant, "invocation_id": inp.invocation_id, "attempt": attempt})
        # body = json_or_none(resp)
        # if resp.status_code != 200 or not isinstance(body, dict) \
        #         or not isinstance(body.get("potential_ofac_match"), bool) or not isinstance(body.get("pep_flag"), bool):
        #     raise errors.transient(errors.SERVICE_UNAVAILABLE, f"unexpected screening response ({resp.status_code})")
        body = {"potential_ofac_match": False, "pep_flag": False, "details": {},
                "provider_reference": "simulated-screening-001"}
        details = body.get("details") or body.get("result_details") or {}
        return ScreeningResult(
            status="SUCCEEDED", potential_ofac_match=body["potential_ofac_match"], pep_flag=body["pep_flag"],
            result_details=details if isinstance(details, dict) else {"details": str(details)[:500]},
            provider_reference=body.get("provider_reference"), attempts=attempt, screened_at=utcnow())
