"""Registers the Search Attributes on a real dev server and runs a clean workflow with them."""
import uuid

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal.models import KycResult, KycWorkflowInput, SubmissionRef
from temporal.cli.search_attributes import ATTRIBUTES, register_search_attributes
from temporal.workflow import KycApplicationWorkflow
from tests.fakes import APP, SUB, Fakes


async def test_register_and_upsert_search_attributes():
    try:
        env = await WorkflowEnvironment.start_local()
    except Exception as exc:  # no network to download the dev server
        pytest.skip(f"dev server unavailable: {exc}")
    async with env:
        await register_search_attributes(env.client)
        await register_search_attributes(env.client)          # idempotent
        f = Fakes()
        async with Worker(env.client, task_queue="sa", workflows=[KycApplicationWorkflow],
                          activities=f.activities()):
            wf_id = f"kyc-{uuid.uuid4()}"
            res = await env.client.execute_workflow(
                KycApplicationWorkflow.run, KycWorkflowInput(APP, SUB, SubmissionRef("b", "k", "b" * 64)),
                id=wf_id, task_queue="sa", result_type=KycResult)
            assert res.status == "ACTIVE"
            desc = await env.client.get_workflow_handle(wf_id).describe()
            names = {p.key.name for p in desc.typed_search_attributes}
            assert {"KycApplicationId", "KycBusinessStatus", "KycStage"} <= names
            assert set(ATTRIBUTES) >= {"KycPolicyVersionInt"}
