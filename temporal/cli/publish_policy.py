"""Publish a policy version to Garage: python -m temporal.cli.publish_policy <version>

Writes and verifies the immutable version object first, then replaces the
current pointer (spec section 7). Refuses to overwrite an existing, different
version object.
"""
import argparse
import hashlib
import json
import sys

from botocore.exceptions import ClientError

from ..config import REPO_ROOT, Settings
from ..activities.storage import Storage


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("version", type=int)
    ap.add_argument("--policy-id", default="onboarding")
    ap.add_argument("--no-pointer", action="store_true", help="upload the version without making it current")
    args = ap.parse_args()

    cfg = Settings.from_env()
    store = Storage(cfg)
    local = (REPO_ROOT / "policies" / args.policy_id / "versions" / f"{args.version}.json").read_bytes()
    doc = json.loads(local)
    if doc.get("policy_id") != args.policy_id or doc.get("version") != args.version:
        sys.exit("policy file identity does not match the requested policy/version")
    digest = hashlib.sha256(local).hexdigest()
    key = f"policies/{args.policy_id}/versions/{args.version}.json"

    try:
        existing = store._client.get_object(Bucket=cfg.policy_bucket, Key=key)["Body"].read()
        if hashlib.sha256(existing).hexdigest() != digest:
            sys.exit(f"{key} already exists with different content; versions are immutable")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in ("NoSuchKey", "404"):
            raise
        store.put_bytes(cfg.policy_bucket, key, local)
    stored = store._client.get_object(Bucket=cfg.policy_bucket, Key=key)["Body"].read()
    if hashlib.sha256(stored).hexdigest() != digest:
        sys.exit("verification of the uploaded version failed; pointer not changed")

    print(f"version {args.version} sha256={digest}")
    if not args.no_pointer:
        pointer = {"policy_id": args.policy_id, "version": args.version, "object_key": key, "sha256": digest}
        store.put_bytes(cfg.policy_bucket, f"policies/{args.policy_id}/current.json",
                        json.dumps(pointer, indent=2).encode())
        print(f"current pointer -> version {args.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
