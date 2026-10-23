"""Activity 6: app_screen_applicant."""
from temporalio import activity

from .. import errors
from ..models import ScreenInput, ScreeningResult
from .base import ActivityBase, json_or_none, utcnow


class ScreenApplicant(ActivityBase):
    @activity.defn(name="app_screen_applicant")
    async def app_screen_applicant(self, inp: ScreenInput) -> ScreeningResult:
        attempt = activity.info().attempt
        applicant = inp.declared_applicant
        resp = await self._post(
            f"{self.cfg.screening_url}/v1/screen", self.cfg.screening_http_timeout, self.cfg.screening_api_key,
            "screening",
            json={"application_id": inp.application_id,
                  "subject": {"name": f"{applicant.get('given_name', '')} {applicant.get('family_name', '')}".strip(),
                              "date_of_birth": applicant.get("date_of_birth"),
                              "country": (applicant.get("residential_address") or {}).get("country_code")},
                  # Demo only: the submission's metadata packet tells the stub which scenario to return.
                  "metadata": inp.stub_metadata or {}})
        body = json_or_none(resp)
        if resp.status_code != 200 or not isinstance(body, dict) or body.get("decision") not in ("pass", "fail"):
            raise errors.transient(errors.SERVICE_UNAVAILABLE, f"unexpected screening response ({resp.status_code})")
        # The stub has a single failing result, treated as a potential OFAC match.
        details = {"reason_code": body.get("reason_code"), "message": body.get("message")}
        return ScreeningResult(
            status="SUCCEEDED", potential_ofac_match=body["decision"] == "fail", pep_flag=False,
            result_details=details, provider_reference=body.get("application_id"), attempts=attempt, screened_at=utcnow())
