"""Operator/reviewer helper that sends validated Updates and Queries.

A stand-in for the authenticated review/operator API: it asserts the actor and
role passed on the command line, so use it only in the demo.

  python -m temporal.cli.operator_cli state     <application_id>
  python -m temporal.cli.operator_cli decide    <application_id> APPROVE|REJECT --actor alice [--request-id R]
  python -m temporal.cli.operator_cli reevaluate <application_id> --version 2 --sha256 H --actor ops
  python -m temporal.cli.operator_cli retry     <application_id> --actor ops
"""
import argparse
import asyncio
import dataclasses
import json
import uuid

from ..config import Settings
from ..models import ProcessingState, ReevaluationRequest, RetryRequest, ReviewDecisionRequest, UpdateOutcome
from ..worker import connect


async def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("state", "decide", "reevaluate", "retry"):
        p = sub.add_parser(name)
        p.add_argument("application_id")
        if name != "state":
            p.add_argument("--actor", required=True)
            p.add_argument("--reason", default="operator action")
            p.add_argument("--request-id", default=None, help="reuse to demonstrate de-duplication")
        if name == "decide":
            p.add_argument("decision", choices=["APPROVE", "REJECT"])
            p.add_argument("--role", default="REVIEWER")
            p.add_argument("--notes")
        if name == "reevaluate":
            p.add_argument("--policy-id", default="onboarding")
            p.add_argument("--version", type=int, required=True)
            p.add_argument("--sha256", required=True)
            p.add_argument("--campaign-id")
    args = ap.parse_args()

    client = await connect(Settings.from_env())
    handle = client.get_workflow_handle(f"kyc-{args.application_id}")
    state: ProcessingState = await handle.query("get_processing_state", result_type=ProcessingState)
    rid = getattr(args, "request_id", None) or str(uuid.uuid4())

    if args.cmd == "state":
        print(json.dumps(dataclasses.asdict(state), indent=2))
        return
    if args.cmd == "decide":
        req = ReviewDecisionRequest(
            request_id=rid, application_id=args.application_id, review_task_id=state.review_task_id or "",
            evaluation_id=state.evaluation_id or "", decision=args.decision, actor_id=args.actor,
            actor_role=args.role, reason=args.reason, notes=args.notes)
        out = await handle.execute_update("submit_review_decision", req, result_type=UpdateOutcome)
    elif args.cmd == "reevaluate":
        req = ReevaluationRequest(
            request_id=rid, application_id=args.application_id, expected_evaluation_id=state.evaluation_id or "",
            policy_id=args.policy_id, policy_version=args.version, policy_sha256=args.sha256,
            actor_id=args.actor, actor_role="OPERATOR", reason=args.reason, campaign_id=args.campaign_id)
        out = await handle.execute_update("request_policy_reevaluation", req, result_type=UpdateOutcome)
    else:
        req = RetryRequest(
            request_id=rid, application_id=args.application_id, failed_stage=state.failed_stage or "",
            recovery_generation=state.recovery_generation, actor_id=args.actor, actor_role="OPERATOR",
            reason=args.reason)
        out = await handle.execute_update("retry_failed_stage", req, result_type=UpdateOutcome)
    print(json.dumps(dataclasses.asdict(out), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
