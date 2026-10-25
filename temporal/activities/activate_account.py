"""Activity 9: app_activate_account."""
from temporalio import activity

from .. import errors
from ..models import ActivateAccountInput, ActivationReceipt
from .base import ActivityBase, error_code, json_or_none, utcnow


class ActivateAccount(ActivityBase):
    @activity.defn(name="app_activate_account")
    async def app_activate_account(self, inp: ActivateAccountInput) -> ActivationReceipt:
        """Calls the account service only; never writes Postgres.

        The idempotency key is application-level (kyc-activate:{application_id}),
        never derived from attempt number or run ID.
        """
        resp = await self._post(
            f"{self.cfg.account_url}/v1/accounts", self.cfg.account_http_timeout, self.cfg.account_api_key,
            "account service",
            headers={"Idempotency-Key": inp.idempotency_key},
            json={"application_id": inp.application_id, "evaluation_id": inp.evaluation_id,
                  "decision": "APPROVED",
                  # Demo only: the submission's metadata packet tells the stub which scenario to return.
                  "metadata": inp.stub_metadata or {}})
        status_code, body = resp.status_code, json_or_none(resp)
        if status_code in (200, 201) and isinstance(body, dict) and body.get("account_id"):
            now = utcnow()
            return ActivationReceipt(
                account_id=str(body["account_id"]), provider_reference=body.get("provider_reference"),
                activated_at=body.get("activated_at") or now, confirmed_at=now)
        if 400 <= status_code < 500:  # includes 409: key reused with different input
            raise errors.fatal(errors.ACTIVATION_REJECTED,
                               f"account service rejected activation ({status_code}, "
                               f"{error_code(body, 'REJECTED')})")
        raise errors.transient(errors.SERVICE_UNAVAILABLE, f"unexpected account response ({status_code})")
