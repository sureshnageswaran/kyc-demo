"""Start a KycApplicationWorkflow by hand (Intake normally does this).

python -m temporal.cli.start_workflow --application-id A --submission-id S --sha256 H [--timer-profile demo-v1]
"""
import argparse
import asyncio

from temporalio.common import SearchAttributePair, TypedSearchAttributes, WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from ..config import Settings
from ..models import KycWorkflowInput, SubmissionRef
from ..worker import connect
from ..workflow import SA_APPLICATION_ID, KycApplicationWorkflow


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--application-id", required=True)
    ap.add_argument("--submission-id", required=True)
    ap.add_argument("--sha256", required=True)
    ap.add_argument("--bucket")
    ap.add_argument("--timer-profile", default="normal-v1")
    args = ap.parse_args()

    cfg = Settings.from_env()
    client = await connect(cfg)
    handle = await client.start_workflow(
        KycApplicationWorkflow.run,
        KycWorkflowInput(
            application_id=args.application_id, submission_id=args.submission_id,
            submission_ref=SubmissionRef(
                bucket=args.bucket or cfg.s3_bucket,
                key=f"applications/{args.application_id}/submission.json", sha256=args.sha256),
            timer_profile=args.timer_profile),
        id=f"kyc-{args.application_id}",
        task_queue=cfg.temporal_task_queue,
        id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        search_attributes=TypedSearchAttributes([SearchAttributePair(SA_APPLICATION_ID, args.application_id)]),
    )
    print(f"started {handle.id} run_id={handle.result_run_id}")


if __name__ == "__main__":
    asyncio.run(main())
