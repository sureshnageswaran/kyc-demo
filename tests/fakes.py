"""In-memory Activities for workflow tests: same names and contracts, no I/O."""
import hashlib
import json
from collections import defaultdict

from temporalio import activity
from temporalio.exceptions import ApplicationError

from temporal import kyc_logic
from temporal.models import (
    ActivateAccountInput, ActivationReceipt, AssessmentPersisted, DocumentOutcome, DocumentRef, EvaluateApplicationInput,
    EvaluateInput, EvaluationResult, FinalOutcomeInput, FinalOutcomePersisted, LoadedSubmission,
    LoadSubmissionInput, ParseDocumentInput, PersistActivationInput, PersistAssessmentInput, PolicySnapshot,
    RecordEventInput, ScreenInput, ScreeningResult, StartProcessingInput,
)

APP = "11111111-1111-4111-8111-111111111111"
SUB = "33333333-3333-4333-8333-333333333333"


def policy_snapshot(version: int, threshold: int) -> PolicySnapshot:
    policy = {"schema_version": "1.0", "policy_id": "onboarding", "version": version, "rules": [{
        "rule_id": "minimum_average_credit_age", "field": "average_credit_age_months", "operator": "gt",
        "threshold": threshold, "on_failure": "REVIEW", "on_missing": "NONE", "on_conflict": "REVIEW"}]}
    digest = hashlib.sha256(json.dumps(policy).encode()).hexdigest()
    return PolicySnapshot("onboarding", version, "b", f"policies/onboarding/versions/{version}.json", digest,
                          policy, "2026-10-20T00:00:00+00:00")


class Fakes:
    def __init__(self, *, doc_fail_attempts=None, screening=(False, False), credit_age=None,
                 activation_fail_first=0, docs=("passport", "utility_bill"), current_threshold=48):
        self.calls = defaultdict(list)
        self.events: list = []
        self.doc_fail_attempts = doc_fail_attempts or {}   # document_type -> attempts that fail transiently
        self.persistent_doc_failure: set[str] = set()
        self.screening = screening
        self.credit_age = credit_age
        self.activation_fail_first = activation_fail_first
        self.docs = docs
        self.current = policy_snapshot(1, current_threshold)
        self.versions = {1: policy_snapshot(1, 48), 2: policy_snapshot(2, 72)}
        self.accounts: dict[str, str] = {}
        self.statuses: list[str] = []
        self.activation_calls = 0

    def activities(self):
        f = self

        @activity.defn(name="app_start_processing")
        async def start(inp: StartProcessingInput) -> None:
            f.events.append(inp.event)

        @activity.defn(name="app_record_event")
        async def record(inp: RecordEventInput) -> None:
            f.events.extend(inp.events)
            f.calls["record"].append(inp)

        @activity.defn(name="app_load_submission")
        async def load(inp: LoadSubmissionInput) -> LoadedSubmission:
            refs = []
            for i, t in enumerate(f.docs):
                did = f"22222222-2222-4222-8222-22222222222{i}"
                refs.append(DocumentRef(APP, did, t, "b", f"applications/{APP}/documents/{did}/content.pdf",
                                        10, "a" * 64, f"ks1:x:a1:d00{i}:o=ok"))
            return LoadedSubmission(APP, SUB, "2026-10-20T00:00:00+00:00",
                                    {"given_name": "Avery", "family_name": "Example", "date_of_birth": "1988-04-16",
                                     "residential_address": {"line1": "1 A St", "city": "X", "region": "NJ",
                                                             "postal_code": "08840"}},
                                    refs, "ks1:x:a1:s=clear")

        @activity.defn(name="app_document_parse")
        async def parse(inp: ParseDocumentInput) -> DocumentOutcome:
            ref = inp.ref
            f.calls["parse"].append((ref.document_type, activity.info().attempt, inp.invocation_id))
            if ref.document_type in f.persistent_doc_failure:
                raise ApplicationError("ocr down", type="ServiceUnavailable")
            if activity.info().attempt in f.doc_fail_attempts.get(ref.document_type, ()):
                raise ApplicationError("ocr 503", type="ServiceUnavailable")
            fields = {"given_name": "Avery", "family_name": "Example", "date_of_birth": "1988-04-16"}
            if ref.document_type == "utility_bill":
                fields = {"line1": "1 A St", "city": "X", "region": "NJ", "postal_code": "08840"}
            if ref.document_type == "other" and f.credit_age is not None:
                fields = {"average_credit_age_months": f.credit_age}
            return DocumentOutcome(ref.document_id, "SUCCEEDED", fields, attempts=activity.info().attempt)

        @activity.defn(name="app_evaluate_application")
        async def evaluate(inp: EvaluateApplicationInput) -> EvaluationResult:
            f.calls["policy"].append(inp.policy_mode)
            policy = f.current if inp.policy_mode == "current" else f.versions[inp.policy_version]
            f.calls["evaluate"].append(policy.version)
            ev = kyc_logic.evaluate(EvaluateInput(
                inp.evaluation_id, inp.evaluation_sequence, inp.declared_applicant, inp.documents, policy,
                inp.evaluated_at))
            ev.policy = policy
            return kyc_logic.apply_screening(ev, inp.screening)

        @activity.defn(name="app_screen_applicant")
        async def screen(inp: ScreenInput) -> ScreeningResult:
            f.calls["screen"].append(activity.info().attempt)
            ofac, pep = f.screening
            return ScreeningResult("SUCCEEDED", ofac, pep, {}, "prov-1", attempts=activity.info().attempt)

        @activity.defn(name="app_persist_assessment")
        async def persist(inp: PersistAssessmentInput) -> AssessmentPersisted:
            f.events.extend(inp.events)
            f.calls["assessment"].append(inp)
            f.statuses.append(inp.target_status)
            return AssessmentPersisted(inp.application_id, inp.target_status)

        @activity.defn(name="app_persist_final_outcome")
        async def final(inp: FinalOutcomeInput) -> FinalOutcomePersisted:
            f.events.extend(inp.events)
            f.calls["final"].append(inp)
            return FinalOutcomePersisted(inp.application_id, inp.decision, inp.decision)

        @activity.defn(name="app_activate_account")
        async def activate(inp: ActivateAccountInput) -> ActivationReceipt:
            f.activation_calls += 1
            if f.activation_calls <= f.activation_fail_first:
                raise ApplicationError("account svc down", type="ServiceUnavailable")
            acct = f.accounts.setdefault(inp.idempotency_key, f"acct-{len(f.accounts) + 1}")
            return ActivationReceipt(acct, "prov", "2026-10-20T00:00:00+00:00", "2026-10-20T00:00:01+00:00")

        @activity.defn(name="app_persist_activation")
        async def persist_activation(inp: PersistActivationInput) -> dict:
            f.events.extend(inp.events)
            f.calls["activation"].append(inp.result)
            return {}

        return [start, record, load, parse, evaluate, screen, persist, final, activate,
                persist_activation]

    def event_types(self):
        return [e.event_type for e in self.events]
