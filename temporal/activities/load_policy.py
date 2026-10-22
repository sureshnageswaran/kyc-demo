"""Activity 4: app_load_policy."""
from temporalio import activity

from .. import errors, kyc_logic
from ..models import LoadPolicyInput, PolicySnapshot
from .base import ActivityBase, utcnow


class LoadPolicy(ActivityBase):
    @activity.defn(name="app_load_policy")
    async def app_load_policy(self, inp: LoadPolicyInput) -> PolicySnapshot:
        bucket = self.cfg.policy_bucket
        if inp.mode == "explicit":
            if inp.version is None or not kyc_logic.is_sha256(inp.sha256):
                raise errors.fatal(errors.POLICY_INVALID, "explicit mode requires a version and sha256")
            version, key, expected_sha = inp.version, f"policies/{inp.policy_id}/versions/{inp.version}.json", inp.sha256
        elif inp.mode == "current":
            pointer, _ = await self.storage.get_json(bucket, f"policies/{inp.policy_id}/current.json")
            try:
                version, key, expected_sha = int(pointer["version"]), pointer["object_key"], pointer["sha256"]
                if pointer["policy_id"] != inp.policy_id or not kyc_logic.is_sha256(expected_sha) \
                        or not key.startswith(f"policies/{inp.policy_id}/versions/"):
                    raise KeyError
            except (KeyError, TypeError, ValueError):
                raise errors.fatal(errors.POLICY_INVALID, "invalid current-policy pointer")
        else:
            raise errors.fatal(errors.POLICY_INVALID, f"unknown policy mode {inp.mode}")

        # No silent fallback to an older version: any failure here propagates.
        policy, digest = await self.storage.get_json(bucket, key)
        if digest != expected_sha:
            raise errors.fatal(errors.POLICY_INVALID, "policy object hash does not match its pointer/request")
        try:
            kyc_logic.validate_policy(policy, expected_id=inp.policy_id, expected_version=version)
        except kyc_logic.PolicyError as exc:
            raise errors.fatal(errors.POLICY_INVALID, str(exc))
        return PolicySnapshot(policy_id=inp.policy_id, version=version, bucket=bucket, object_key=key,
                              sha256=digest, policy=policy, resolved_at=utcnow())
