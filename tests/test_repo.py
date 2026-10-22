"""Transaction/idempotency tests against a real Postgres.

Set KYC_TEST_DATABASE_URL to a scratch database that already has
sql/001_kyc_schema.sql applied (tests create their own applications).
"""
import os
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row
from temporalio.exceptions import ApplicationError

from temporal import kyc_logic
from temporal.activities import repo
from temporal.models import (
    ActivationReceipt, AuditEvent, DocumentOutcome, EvaluateInput, EvaluationDocument, FinalOutcomeInput,
    LoadedSubmission, PersistActivationInput, PersistAssessmentInput, ReviewTaskSpec, ScreeningResult,
    StartProcessingInput, SubmissionRef, RecordEventInput, ReviewMilestone, Projection,
)
from tests.fakes import policy_snapshot

URL = os.environ.get("KYC_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="KYC_TEST_DATABASE_URL not set")

NOW = "2026-10-20T10:00:00+00:00"


@pytest.fixture
async def cur():
    async with await psycopg.AsyncConnection.connect(URL, row_factory=dict_row) as conn:
        async with conn.cursor() as c:
            yield c, conn
        await conn.rollback()


class Ids:
    def __init__(self):
        self.app, self.sub, self.ev, self.task = (str(uuid.uuid4()) for _ in range(4))
        self.doc = str(uuid.uuid4())
        self.wf, self.run = f"kyc-{self.app}", str(uuid.uuid4())
        self._n = 0

    def event(self, kind, key=None, **details):
        self._n += 1
        return AuditEvent(f"{kind}:{key or self._n}", self._n, kind, NOW, details=details)


def start_input(i: Ids, sha="b" * 64):
    return StartProcessingInput(i.app, i.sub, SubmissionRef("kyc-demo", f"applications/{i.app}/submission.json", sha),
                                i.wf, i.run, i.run, NOW, i.event("PROCESSING_STARTED", i.app))


def assessment(i: Ids, review=True, status="AWAITING_REVIEW"):
    from temporal.models import DocumentRef
    ref = DocumentRef(i.app, i.doc, "passport", "kyc-demo", f"applications/{i.app}/documents/{i.doc}/c.pdf", 10,
                      "a" * 64, "ks1:x", "p.pdf")
    policy = policy_snapshot(2, 72)
    ev = kyc_logic.evaluate(EvaluateInput(
        i.ev, 1, {"given_name": "A", "family_name": "B", "date_of_birth": "1990-01-01"},
        [EvaluationDocument(i.doc, "passport", "SUCCEEDED",
                            {"given_name": "A", "family_name": "B", "date_of_birth": "1990-01-01"})], policy, NOW))
    ev.policy = policy
    rv = ReviewTaskSpec(i.task, i.ev, "review-1", "PEP_FLAG", NOW, "2026-10-20T12:00:00+00:00",
                        "2026-10-20T18:00:00+00:00", "2026-10-21T10:00:00+00:00") if review else None
    return PersistAssessmentInput(
        i.app, i.wf, i.run, status, "HUMAN_REVIEW", [i.event("ASSESSMENT_PERSISTED", i.ev)],
        loaded=LoadedSubmission(i.app, i.sub, NOW, {"given_name": "A"}, [], "ks1:ref"),
        documents=[ref], document_outcomes=[DocumentOutcome(i.doc, "SUCCEEDED", {"given_name": "A"}, attempts=1)],
        screening=ScreeningResult("SUCCEEDED", False, True, {}, "p1"), evaluation=ev, review=rv,
        review_deadline_at=rv.expires_at if rv else None)


def final(i: Ids, decision="APPROVED", source="HUMAN"):
    return FinalOutcomeInput(
        i.app, i.wf, i.run, i.ev, decision, source, "R", "alice", NOW, [i.event(f"KYC_{decision}", i.app)],
        review_task_id=i.task if source == "HUMAN" else None, review_request_id=str(uuid.uuid4()),
        activation_idempotency_key=f"kyc-activate:{i.app}" if decision == "APPROVED" else None)


async def test_registration_is_idempotent_and_rejects_identity_conflict(cur):
    c, _ = cur
    i = Ids()
    first = start_input(i)
    await repo.start_processing(c, first)
    await c.execute("UPDATE kyc.applications SET current_stage='DOCUMENT_PARSING' WHERE application_id=%s", (i.app,))
    await repo.start_processing(c, first)                      # retry: same input, same row, state untouched
    await c.execute("SELECT current_stage FROM kyc.applications WHERE application_id=%s", (i.app,))
    assert (await c.fetchone())["current_stage"] == "DOCUMENT_PARSING"
    with pytest.raises(ApplicationError) as exc:
        await repo.start_processing(c, start_input(i, sha="c" * 64))
    assert exc.value.non_retryable


async def test_assessment_review_decision_and_activation_flow(cur):
    c, _ = cur
    i = Ids()
    await repo.start_processing(c, start_input(i))
    a = assessment(i)
    await repo.persist_assessment(c, a)
    await repo.persist_assessment(c, a)                         # retry is a no-op
    await c.execute("SELECT count(*) n FROM kyc.evaluations WHERE application_id=%s", (i.app,))
    assert (await c.fetchone())["n"] == 1
    await c.execute("SELECT status, created_at FROM kyc.review_tasks WHERE application_id=%s", (i.app,))
    task = await c.fetchone()
    assert task["status"] == "OPEN" and task["created_at"].isoformat() == NOW   # supplied opened_at

    approve = final(i)
    out = await repo.persist_final_outcome(c, approve)
    assert out.application_status == "APPROVED"
    await repo.persist_final_outcome(c, approve)               # identical repeat: success, no duplication
    with pytest.raises(ApplicationError):
        await repo.persist_final_outcome(c, final(i, "REJECTED"))   # contradiction is an integrity error
    await c.execute("SELECT status FROM kyc.account_activations WHERE application_id=%s", (i.app,))
    assert (await c.fetchone())["status"] == "PENDING"

    with pytest.raises(ApplicationError):                       # stale assessment cannot regress approval
        await repo.persist_assessment(c, assessment(i))

    act = PersistActivationInput(i.app, i.wf, i.run, "ACTIVE", [i.event("ACTIVATION_CONFIRMED", i.app)],
                                 receipt=ActivationReceipt("acct-1", "p", NOW, NOW))
    await repo.persist_activation(c, act)
    await repo.persist_activation(c, act)
    await c.execute("SELECT status, completed_at FROM kyc.applications WHERE application_id=%s", (i.app,))
    row = await c.fetchone()
    assert row["status"] == "ACTIVE" and row["completed_at"] is not None


async def test_expired_review_cannot_be_approved(cur):
    c, _ = cur
    i = Ids()
    await repo.start_processing(c, start_input(i))
    await repo.persist_assessment(c, assessment(i))
    await repo.record_event(c, RecordEventInput(
        i.app, i.wf, i.run, [i.event("REVIEW_EXPIRED", i.task)], Projection(),
        ReviewMilestone(i.task, "EXPIRY", "2026-10-21T10:00:00+00:00")))
    await c.execute("SELECT status FROM kyc.applications WHERE application_id=%s", (i.app,))
    assert (await c.fetchone())["status"] == "REVIEW_EXPIRED"
    with pytest.raises(ApplicationError):
        await repo.persist_final_outcome(c, final(i))
    await c.execute("SELECT count(*) n FROM kyc.decisions WHERE application_id=%s", (i.app,))
    assert (await c.fetchone())["n"] == 0


async def test_partial_failure_persists_without_fabricating_evaluation(cur):
    c, _ = cur
    i = Ids()
    await repo.start_processing(c, start_input(i))
    inp = assessment(i, review=False, status="PROCESSING_ERROR")
    inp.evaluation = None
    inp.screening = ScreeningResult("NOT_RUN")
    inp.document_outcomes = [DocumentOutcome(i.doc, "TECHNICAL_ERROR", error_code="ServiceUnavailable")]
    inp.error_code, inp.error_message = "ServiceUnavailable", "ocr down"
    await repo.persist_assessment(c, inp)
    await c.execute("SELECT status, last_error_code FROM kyc.applications WHERE application_id=%s", (i.app,))
    assert dict(await c.fetchone()) == {"status": "PROCESSING_ERROR", "last_error_code": "ServiceUnavailable"}
    await c.execute("SELECT count(*) n FROM kyc.evaluations WHERE application_id=%s", (i.app,))
    assert (await c.fetchone())["n"] == 0


async def test_audit_events_are_append_only_and_conflicts_detected(cur):
    c, conn = cur
    i = Ids()
    await repo.start_processing(c, start_input(i))
    ev = i.event("STAGE_STARTED", "X", stage="A")
    await repo.record_event(c, RecordEventInput(i.app, i.wf, i.run, [ev]))
    await repo.record_event(c, RecordEventInput(i.app, i.wf, i.run, [ev]))     # exact retry ok
    ev2 = AuditEvent(ev.event_key, ev.event_sequence, ev.event_type, NOW, details={"stage": "B"})
    with pytest.raises(ApplicationError):
        await repo.record_event(c, RecordEventInput(i.app, i.wf, i.run, [ev2]))
    with pytest.raises(psycopg.errors.RestrictViolation):
        await c.execute("DELETE FROM kyc.audit_events WHERE application_id=%s", (i.app,))
