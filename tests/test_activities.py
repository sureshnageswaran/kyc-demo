"""Activity edge cases with a fake Garage and mocked HTTP stubs (no network)."""
import hashlib
import json

import httpx
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from temporal import errors
from temporal.activities import KycActivities
from temporal.config import Settings
from temporal.models import DocumentRef, LoadPolicyInput, LoadSubmissionInput, ParseDocumentInput, SubmissionRef
from tests.fakes import APP, SUB

BYTES = b"KYC-SEED-FIXTURE-1\n{}\n"


def settings() -> Settings:
    return Settings(
        temporal_address="x", temporal_namespace="default", temporal_task_queue="q", temporal_api_key=None,
        temporal_tls=False, max_concurrent_activities=1, max_concurrent_workflow_tasks=1, database_url="x",
        db_pool_max=1, s3_endpoint_url="x", s3_access_key_id="x", s3_secret_access_key="x", s3_region="r",
        s3_bucket="b", s3_addressing_style="path", policy_bucket="b", ocr_url="http://ocr", ocr_api_key=None,
        screening_url="http://scr", screening_api_key=None, account_url="http://acct", account_api_key=None,
        ocr_http_timeout=5, screening_http_timeout=5, account_http_timeout=5, max_document_bytes=1000)


class FakeStorage:
    def __init__(self, objects):
        self.objects = {k: v if isinstance(v, bytes) else json.dumps(v).encode() for k, v in objects.items()}

    async def get_bytes(self, bucket, key, max_bytes=None):
        if key not in self.objects:
            raise errors.fatal(errors.DOCUMENT_INTEGRITY, "missing")
        return self.objects[key]

    async def get_json(self, bucket, key):
        data = await self.get_bytes(bucket, key)
        return json.loads(data), hashlib.sha256(data).hexdigest()


def acts(storage, handler=None):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler or (lambda r: httpx.Response(200, json={}))))
    return KycActivities(settings(), None, storage, http)


def manifest(**over):
    doc_id = "22222222-2222-4222-8222-222222222222"
    m = {"schema_version": "1.0", "application_id": APP, "submission_id": SUB, "submitted_at": "t",
         "client_reference": "ks1:x:a1:s=clear", "applicant": {"given_name": "A"},
         "documents": [{"document_id": doc_id, "document_type": "passport", "size_bytes": 10, "sha256": "a" * 64,
                        "object_key": f"applications/{APP}/documents/{doc_id}/content.pdf"}]}
    m.update(over)
    return m


async def load(storage_obj, sha=None):
    key = f"applications/{APP}/submission.json"
    data = json.dumps(storage_obj).encode()
    ref = SubmissionRef("b", key, sha or hashlib.sha256(data).hexdigest())
    return await ActivityEnvironment().run(
        acts(FakeStorage({key: storage_obj})).app_load_submission, LoadSubmissionInput(APP, SUB, ref))


async def test_load_submission_ok_and_validations():
    res = await load(manifest())
    assert res.client_reference == "ks1:x:a1:s=clear" and len(res.documents) == 1
    with pytest.raises(ApplicationError):
        await load(manifest(), sha="0" * 64)                                    # hash mismatch
    with pytest.raises(ApplicationError):
        await load(manifest(submission_id="x"))                                 # identity mismatch
    other = manifest()
    other["documents"][0]["object_key"] = "applications/other/documents/x/content.pdf"
    with pytest.raises(ApplicationError):
        await load(other)                                                       # foreign document
    dup = manifest()
    dup["documents"].append(dict(dup["documents"][0]))
    with pytest.raises(ApplicationError):
        await load(dup)                                                         # duplicate document ids


def policy_objects(version=2, tamper=False):
    policy = {"schema_version": "1.0", "policy_id": "onboarding", "version": version, "rules": [{
        "rule_id": "r", "field": "f", "operator": "gt", "threshold": 1}]}
    body = json.dumps(policy).encode()
    sha = hashlib.sha256(body).hexdigest()
    key = f"policies/onboarding/versions/{version}.json"
    pointer = {"policy_id": "onboarding", "version": version, "object_key": key, "sha256": "0" * 64 if tamper else sha}
    return {key: body, "policies/onboarding/current.json": pointer}, sha


async def test_policy_current_and_explicit_modes_and_hash_checks():
    objs, sha = policy_objects()
    a = acts(FakeStorage(objs))
    snap = await ActivityEnvironment().run(a.app_load_policy, LoadPolicyInput("onboarding"))
    assert snap.version == 2 and snap.sha256 == sha
    snap = await ActivityEnvironment().run(a.app_load_policy, LoadPolicyInput("onboarding", "explicit", 2, sha))
    assert snap.version == 2
    for bad in (LoadPolicyInput("onboarding", "explicit", 2, "f" * 64),):
        with pytest.raises(ApplicationError):
            await ActivityEnvironment().run(a.app_load_policy, bad)
    bad_objs, _ = policy_objects(tamper=True)
    with pytest.raises(ApplicationError):                                       # no silent fallback
        await ActivityEnvironment().run(acts(FakeStorage(bad_objs)).app_load_policy, LoadPolicyInput("onboarding"))


def doc_ref():
    return DocumentRef(APP, "d1", "passport", "b", "k", len(BYTES), hashlib.sha256(BYTES).hexdigest(), "ks1:x")


async def parse(status, body=None, store=None):
    a = acts(FakeStorage({"k": BYTES} if store is None else store), lambda r: httpx.Response(status, json=body or {}))
    return await ActivityEnvironment().run(a.app_document_parse, ParseDocumentInput(doc_ref(), "inv-1"))


async def test_document_parse_outcome_mapping():
    ok = await parse(200, {"fixture_fields": {"given_name": "A"}})
    assert ok.status == "SUCCEEDED" and ok.extracted_fields == {"given_name": "A"}
    assert (await parse(400, {"error": {"code": "CORRUPT_PDF"}})).status == "DOCUMENT_ERROR"
    assert (await parse(422)).error_code == "UNREADABLE_DOCUMENT"
    with pytest.raises(ApplicationError) as e:
        await parse(503)
    assert not e.value.non_retryable                                            # retryable
    with pytest.raises(ApplicationError) as e:
        await parse(200, {"fixture_fields": {}}, store={"k": BYTES + b"x"})     # checksum/size mismatch
    assert e.value.non_retryable and e.value.type == errors.DOCUMENT_INTEGRITY


async def test_activation_idempotency_header_and_rejection():
    seen = {}

    def handler(req):
        seen["key"] = req.headers["Idempotency-Key"]
        return httpx.Response(409, json={"error": {"code": "KEY_REUSED"}})

    from temporal.models import ActivateAccountInput
    a = acts(FakeStorage({}), handler)
    with pytest.raises(ApplicationError) as e:
        await ActivityEnvironment().run(a.app_activate_account, ActivateAccountInput(APP, f"kyc-activate:{APP}", "e"))
    assert seen["key"] == f"kyc-activate:{APP}" and e.value.type == errors.ACTIVATION_REJECTED
