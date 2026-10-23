import dataclasses
import uuid

import pytest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal.models import (
    KycResult, KycWorkflowInput, ProcessingState, ReevaluationRequest, RetryRequest, ReviewDecisionRequest, UpdateOutcome,
    SubmissionRef,
)
from temporal.workflow import KycApplicationWorkflow
from tests.fakes import APP, SUB, Fakes

QUEUE = "kyc-test"


@pytest.fixture(autouse=True)
def no_search_attributes(monkeypatch):
    # The time-skipping test server cannot register custom Search Attributes;
    # they are exercised against the dev server in test_e2e_dev_server.py.
    monkeypatch.setattr(KycApplicationWorkflow, "_sync_search_attributes", lambda self: None)


@pytest.fixture(scope="module")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


async def start(env, fakes, profile="demo-v1"):
    worker = Worker(env.client, task_queue=QUEUE, workflows=[KycApplicationWorkflow],
                    activities=fakes.activities(),
                    workflow_runner=UnsandboxedWorkflowRunner())
    wf_id = f"kyc-{uuid.uuid4()}"
    inp = KycWorkflowInput(APP, SUB, SubmissionRef("b", "k", "b" * 64), timer_profile=profile)
    return worker, wf_id, inp


async def test_clean_application_activates_once(env):
    f = Fakes()
    worker, wf_id, inp = await start(env, f)
    async with worker:
        res = await env.client.execute_workflow(KycApplicationWorkflow.run, inp, id=wf_id, task_queue=QUEUE,
                                                result_type=KycResult)
    assert res.status == "ACTIVE" and res.account_id == "acct-1"
    assert f.calls["final"][0].decision_source == "AUTOMATIC"
    assert f.calls["policy"] == ["current"]
    types = f.event_types()
    for t in ("PROCESSING_STARTED", "POLICY_RESOLVED", "EVALUATION_COMPLETED", "KYC_APPROVED",
              "ACTIVATION_REQUESTED", "ACTIVATION_CONFIRMED"):
        assert t in types
    seqs = [e.event_sequence for e in f.events]
    assert len(seqs) == len(set(seqs))  # workflow-assigned, unique per run


async def test_transient_ocr_failure_retries_only_that_document(env):
    f = Fakes(doc_fail_attempts={"passport": (1, 2)})
    worker, wf_id, inp = await start(env, f)
    async with worker:
        res = await env.client.execute_workflow(KycApplicationWorkflow.run, inp, id=wf_id, task_queue=QUEUE,
                                                result_type=KycResult)
    assert res.status == "ACTIVE"
    attempts = [(t, a) for t, a, _ in f.calls["parse"]]
    assert attempts.count(("passport", 3)) == 1 and sum(1 for t, _ in attempts if t == "utility_bill") == 1
    ids = {i for t, _, i in f.calls["parse"] if t == "passport"}
    assert len(ids) == 1  # invocation id is stable across retries


async def test_pep_opens_review_and_approval_activates(env):
    f = Fakes(screening=(False, True))
    worker, wf_id, inp = await start(env, f)
    async with worker:
        handle = await env.client.start_workflow(KycApplicationWorkflow.run, inp, id=wf_id, task_queue=QUEUE)
        await env.sleep(1)
        st = await handle.query("get_processing_state", result_type=ProcessingState)
        assert st.business_status == "AWAITING_REVIEW" and st.review_deadline_at
        assert "submit_review_decision" in st.available_actions
        req = ReviewDecisionRequest("req-1", APP, st.review_task_id, st.evaluation_id, "APPROVE", "alice",
                                    "REVIEWER", "pep cleared")
        out = await handle.execute_update("submit_review_decision", req, result_type=UpdateOutcome)
        again = await handle.execute_update("submit_review_decision", req, result_type=UpdateOutcome)       # exact duplicate
        assert again == out
        with pytest.raises(WorkflowUpdateFailedError):                            # same id, new body
            await handle.execute_update("submit_review_decision", dataclasses.replace(req, decision="REJECT"))
        with pytest.raises(WorkflowUpdateFailedError):                            # bad role
            await handle.execute_update("submit_review_decision",
                                        dataclasses.replace(req, request_id="req-2", actor_role="VIEWER"))
        res = await handle.result()
    assert res.status == "ACTIVE"
    assert f.event_types().count("REVIEW_DECISION_ACCEPTED") == 1
    assert f.calls["final"][0].decision_source == "HUMAN"


async def test_unanswered_review_reminds_escalates_expires(env):
    f = Fakes(screening=(True, False))
    worker, wf_id, inp = await start(env, f)
    async with worker:
        res = await env.client.execute_workflow(KycApplicationWorkflow.run, inp, id=wf_id, task_queue=QUEUE,
                                                result_type=KycResult)
    assert res.status == "REVIEW_EXPIRED"
    types = f.event_types()
    i = [types.index(t) for t in ("REVIEW_REMINDER_DUE", "REVIEW_ESCALATED", "REVIEW_EXPIRED")]
    assert i == sorted(i)
    assert not f.calls["final"] and f.activation_calls == 0 and "KYC_APPROVED" not in types


async def test_late_decision_after_expiry_is_rejected(env):
    f = Fakes(screening=(False, True))
    worker, wf_id, inp = await start(env, f)
    async with worker:
        handle = await env.client.start_workflow(KycApplicationWorkflow.run, inp, id=wf_id, task_queue=QUEUE)
        await env.sleep(1)
        st = await handle.query("get_processing_state", result_type=ProcessingState)
        assert await handle.result() is not None  # time skips past expiry
        with pytest.raises(Exception):
            await handle.execute_update("submit_review_decision", ReviewDecisionRequest(
                "late", APP, st.review_task_id, st.evaluation_id, "APPROVE", "alice", "REVIEWER", "late"))
    assert f.activation_calls == 0


async def test_reevaluation_supersedes_review_and_pins_v2(env):
    f = Fakes(credit_age=60, docs=("passport", "utility_bill", "other"), screening=(False, False))
    f.current = f.versions[1]
    worker, wf_id, inp = await start(env, f, profile="normal-v1")
    async with worker:
        handle = await env.client.start_workflow(KycApplicationWorkflow.run, inp, id=wf_id, task_queue=QUEUE)
        # v1 (48): 60 passes -> automatic approval and activation, nothing to re-evaluate
        res = await handle.result()
        assert res.status == "ACTIVE" and f.calls["evaluate"] == [1]

    f2 = Fakes(credit_age=60, docs=("passport", "utility_bill", "other"), screening=(False, True))
    f2.current = f2.versions[1]
    worker, wf_id, inp = await start(env, f2, profile="normal-v1")
    async with worker:
        handle = await env.client.start_workflow(KycApplicationWorkflow.run, inp, id=wf_id, task_queue=QUEUE)
        await env.sleep(1)
        st = await handle.query("get_processing_state", result_type=ProcessingState)
        old_task = st.review_task_id
        v2 = f2.versions[2]
        req = ReevaluationRequest("re-1", APP, st.evaluation_id, "onboarding", 2, v2.sha256, "ops", "OPERATOR",
                                  "policy v2", "camp-1")
        out = await handle.execute_update("request_policy_reevaluation", req, result_type=UpdateOutcome)
        assert out.state == "REEVALUATED" and out.detail["review_required"]
        assert f2.calls["policy"][-1] == "explicit" and f2.calls["evaluate"] == [1, 2]
        persisted = f2.calls["assessment"][-1]
        assert persisted.supersede_review_task_id == old_task
        assert persisted.review.expires_at == f2.calls["assessment"][0].review.expires_at  # budget carried
        new_st = await handle.query("get_processing_state", result_type=ProcessingState)
        assert new_st.policy_version == 2 and new_st.evaluation_id != st.evaluation_id
        # the old evaluation/task is stale now
        with pytest.raises(WorkflowUpdateFailedError):
            await handle.execute_update("submit_review_decision", ReviewDecisionRequest(
                "stale", APP, old_task, st.evaluation_id, "APPROVE", "alice", "REVIEWER", "x"))
        await handle.execute_update("submit_review_decision", ReviewDecisionRequest(
            "ok", APP, new_st.review_task_id, new_st.evaluation_id, "REJECT", "alice", "REVIEWER", "no"))
        assert (await handle.result()).status == "REJECTED"
    assert f2.activation_calls == 0


async def test_decision_rereads_policy_and_supersedes_when_a_new_one_is_published(env):
    f = Fakes(credit_age=60, docs=("passport", "utility_bill", "other"), screening=(False, True))
    f.current = f.versions[1]
    worker, wf_id, inp = await start(env, f, profile="normal-v1")
    async with worker:
        handle = await env.client.start_workflow(KycApplicationWorkflow.run, inp, id=wf_id, task_queue=QUEUE)
        await env.sleep(1)
        st = await handle.query("get_processing_state", result_type=ProcessingState)
        # a newer policy is published while the application waits in review
        f.current = f.versions[2]
        with pytest.raises(WorkflowUpdateFailedError) as exc:
            await handle.execute_update("submit_review_decision", ReviewDecisionRequest(
                "d1", APP, st.review_task_id, st.evaluation_id, "APPROVE", "alice", "REVIEWER", "ok"))
        assert exc.value.cause.type == "PolicyChanged"
        new_st = await handle.query("get_processing_state", result_type=ProcessingState)
        assert new_st.policy_version == 2 and new_st.evaluation_id != st.evaluation_id
        assert new_st.review_task_id != st.review_task_id and f.calls["assessment"][-1].supersede_review_task_id
        # deciding against the new evaluation succeeds; v2 is now the seen current policy
        await handle.execute_update("submit_review_decision", ReviewDecisionRequest(
            "d2", APP, new_st.review_task_id, new_st.evaluation_id, "REJECT", "alice", "REVIEWER", "no"))
        assert (await handle.result()).status == "REJECTED"
    assert f.activation_calls == 0


async def test_technical_document_failure_waits_for_operator_then_retries_only_failed(env):
    f = Fakes()
    f.persistent_doc_failure = {"passport"}
    worker, wf_id, inp = await start(env, f)
    async with worker:
        handle = await env.client.start_workflow(KycApplicationWorkflow.run, inp, id=wf_id, task_queue=QUEUE)
        await env.sleep(120)
        st = await handle.query("get_processing_state", result_type=ProcessingState)
        assert st.business_status == "PROCESSING_ERROR" and st.waiting_reason == "OPERATOR_RECOVERY"
        assert st.documents_technical_error == 1 and st.documents_succeeded == 1
        assert f.statuses[-1] == "PROCESSING_ERROR" and not f.calls["evaluate"]   # nothing fabricated
        f.persistent_doc_failure = set()
        await handle.execute_update("retry_failed_stage", RetryRequest(
            "retry-1", APP, st.failed_stage, st.recovery_generation, "ops", "OPERATOR", "ocr fixed"))
        with pytest.raises(WorkflowUpdateFailedError):   # generation moved on
            await handle.execute_update("retry_failed_stage", RetryRequest(
                "retry-2", APP, st.failed_stage, st.recovery_generation, "ops", "OPERATOR", "again"))
        assert (await handle.result()).status == "ACTIVE"
    assert sum(1 for t, _, _ in f.calls["parse"] if t == "utility_bill") == 1  # sibling not re-parsed


async def test_recovery_wait_expires_without_activation(env):
    f = Fakes()
    f.persistent_doc_failure = {"passport"}
    worker, wf_id, inp = await start(env, f)
    async with worker:
        res = await env.client.execute_workflow(KycApplicationWorkflow.run, inp, id=wf_id, task_queue=QUEUE,
                                                result_type=KycResult)
    assert res.status == "PROCESSING_ERROR"
    assert "RECOVERY_WAIT_EXPIRED" in f.event_types() and f.activation_calls == 0


async def test_activation_failure_keeps_approval_and_operator_retry_reuses_key(env):
    f = Fakes(activation_fail_first=5)   # exhausts the 5 attempts of generation 0
    worker, wf_id, inp = await start(env, f)
    async with worker:
        handle = await env.client.start_workflow(KycApplicationWorkflow.run, inp, id=wf_id, task_queue=QUEUE)
        await env.sleep(120)
        st = await handle.query("get_processing_state", result_type=ProcessingState)
        assert st.business_status == "ACTIVATION_ERROR" and st.failed_stage == "ACTIVATION"
        assert f.calls["activation"] == ["TECHNICAL_ERROR"]
        await handle.execute_update("retry_failed_stage", RetryRequest(
            "retry-a", APP, "ACTIVATION", st.recovery_generation, "ops", "OPERATOR", "service restored"))
        res = await handle.result()
    assert res.status == "ACTIVE"
    assert len(f.accounts) == 1 and list(f.accounts) == [f"kyc-activate:{APP}"]
    assert "ACTIVATION_ERROR" in f.event_types() and "OPERATOR_RETRY_REQUESTED" in f.event_types()
