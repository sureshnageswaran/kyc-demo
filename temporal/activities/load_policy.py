"""Policy resolution, called from app_evaluate_application (no longer a separate Activity)."""
from .. import errors, kyc_logic
from ..models import LoadPolicyInput, PolicySnapshot
from .base import ActivityBase, utcnow


class LoadPolicy(ActivityBase):
    async def resolve_policy(self, inp: LoadPolicyInput) -> PolicySnapshot:
        """Resolve one policy version from object storage and return it as an immutable snapshot.

        Two modes decide *which* version to load; both then share the same fetch/verify path:
          current  - follow policies/<id>/current.json, a pointer naming the version, its object
                     key and the expected sha256 (used for a new application's first evaluation).
          explicit - the caller names the version and sha256 (used by re-evaluation, so it never
                     drifts to a newer "current").
        Policy objects live in policies/<id>/versions/<n>.json. Read-only: no Postgres writes.
        """
        bucket = self.cfg.policy_bucket
        if inp.mode == "explicit":
            # Version and hash must both be supplied: the hash pins the exact bytes.
            if inp.version is None or not kyc_logic.is_sha256(inp.sha256):
                raise errors.fatal(errors.POLICY_INVALID, "explicit mode requires a version and sha256")
            version, key, expected_sha = inp.version, f"policies/{inp.policy_id}/versions/{inp.version}.json", inp.sha256
        elif inp.mode == "current":
            # Read the pointer, then validate its shape before trusting any of it.
            pointer, _ = await self.storage.get_json(bucket, f"policies/{inp.policy_id}/current.json")
            try:
                version, key, expected_sha = int(pointer["version"]), pointer["object_key"], pointer["sha256"]
                # The pointer must name this policy, carry a real sha256, and only point inside
                # this policy's versions/ prefix (no redirecting to another policy's objects).
                if pointer["policy_id"] != inp.policy_id or not kyc_logic.is_sha256(expected_sha) \
                        or not key.startswith(f"policies/{inp.policy_id}/versions/"):
                    raise KeyError  # funnel every shape problem into the single error below
            except (KeyError, TypeError, ValueError):
                raise errors.fatal(errors.POLICY_INVALID, "invalid current-policy pointer")
        else:
            raise errors.fatal(errors.POLICY_INVALID, f"unknown policy mode {inp.mode}")

        # No silent fallback to an older version: any failure here propagates.
        policy, digest = await self.storage.get_json(bucket, key)
        # digest is the sha256 of the stored bytes; a mismatch means the object changed after the
        # pointer/request was written (tampering or a bad publish), so refuse it.
        if digest != expected_sha:
            raise errors.fatal(errors.POLICY_INVALID, "policy object hash does not match its pointer/request")
        # Structural check: schema_version, matching policy_id/version, well-formed rules.
        try:
            kyc_logic.validate_policy(policy, expected_id=inp.policy_id, expected_version=version)
        except kyc_logic.PolicyError as exc:
            raise errors.fatal(errors.POLICY_INVALID, str(exc))
        # The snapshot embeds the full policy plus its bucket/key/sha256, so the workflow and audit
        # trail record exactly which bytes were evaluated against.
        return PolicySnapshot(policy_id=inp.policy_id, version=version, bucket=bucket, object_key=key,
                              sha256=digest, policy=policy, resolved_at=utcnow())
