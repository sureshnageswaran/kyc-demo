"""Transactional Postgres writes for the KYC workflow (spec sections 5, 10, 15, 18).

Every function runs inside one transaction supplied by the caller, locks the
application row, is idempotent under Activity retry, and appends its audit
events in that same transaction. Integrity conflicts raise a non-retryable
ApplicationError so Temporal does not retry them.
"""
import json
from typing import Any

from psycopg import AsyncCursor
from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation
from psycopg.types.json import Jsonb

from .. import errors
from ..models import (
    AssessmentPersisted,
    AuditEvent,
    FinalOutcomeInput,
    FinalOutcomePersisted,
    PersistActivationInput,
    PersistAssessmentInput,
    RecordEventInput,
    StartProcessingInput,
)

FINAL_STATUSES = {"REJECTED", "ACTIVE", "REVIEW_EXPIRED"}
POST_APPROVAL_STATUSES = {"APPROVED", "ACTIVATING", "ACTIVATION_ERROR", "ACTIVE"}
OPEN_REVIEW_STATUSES = ("OPEN", "ASSIGNED", "ESCALATED")

PROJECTION_COLUMNS = {
    "status", "current_stage", "waiting_reason", "stage_started_at",
    "review_deadline_at", "activation_deadline_at", "last_error_code",
    "last_error_message", "completed_at",
}


def integrity(message: str):
    return errors.fatal(errors.INTEGRITY, message)


def check_status_transition(current: str, new: str) -> None:
    """Refuse writes that would regress a finalized or approved application."""
    if new == current:
        return
    if current in FINAL_STATUSES:
        raise integrity(f"application is {current}; refusing change to {new}")
    if current in POST_APPROVAL_STATUSES and new not in POST_APPROVAL_STATUSES:
        raise integrity(f"application is {current}; refusing regression to {new}")


async def lock_application(cur: AsyncCursor, application_id: str) -> dict[str, Any]:
    await cur.execute("SELECT * FROM kyc.applications WHERE application_id = %s FOR UPDATE",
                      (application_id,))
    row = await cur.fetchone()
    if row is None:
        raise integrity(f"application {application_id} is not registered")
    return row


async def insert_events(cur: AsyncCursor, application_id: str, workflow_id: str,
                        run_id: str, events: list[AuditEvent]) -> None:
    for ev in events:
        await cur.execute(
            """
            INSERT INTO kyc.audit_events (
                application_id, event_key, event_sequence, event_type, workflow_id,
                run_id, activity_id, request_id, actor_type, actor_id, reason,
                occurred_at, details)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (application_id, ev.event_key, ev.event_sequence, ev.event_type, workflow_id,
             run_id, ev.activity_id, ev.request_id, ev.actor_type, ev.actor_id, ev.reason,
             ev.occurred_at, Jsonb(ev.details)))
        if cur.rowcount == 0:
            # Retry path: the stored event must match immutable identity/details.
            await cur.execute(
                "SELECT event_type, event_sequence, request_id, actor_id, details "
                "FROM kyc.audit_events WHERE application_id = %s AND event_key = %s",
                (application_id, ev.event_key))
            row = await cur.fetchone()
            same = (row is not None
                    and row["event_type"] == ev.event_type
                    and row["event_sequence"] == ev.event_sequence
                    and row["request_id"] == ev.request_id
                    and row["actor_id"] == ev.actor_id
                    and json.dumps(row["details"], sort_keys=True) == json.dumps(ev.details, sort_keys=True))
            if not same:
                raise integrity(f"audit event {ev.event_key} conflicts with the stored event")


async def _update_application(cur: AsyncCursor, application_id: str, fields: dict[str, Any]) -> None:
    unknown = set(fields) - PROJECTION_COLUMNS
    if unknown:
        raise integrity(f"unsupported projection columns: {sorted(unknown)}")
    if not fields:
        await cur.execute("UPDATE kyc.applications SET updated_at = now() WHERE application_id = %s",
                          (application_id,))
        return
    assignments = ", ".join(f"{col} = %s" for col in fields)
    await cur.execute(
        f"UPDATE kyc.applications SET {assignments}, updated_at = now() WHERE application_id = %s",
        (*fields.values(), application_id))


# ---- 5.1 app_start_processing ----------------------------------------------


async def start_processing(cur: AsyncCursor, inp: StartProcessingInput) -> dict[str, Any]:
    try:
        await cur.execute(
            """
            INSERT INTO kyc.applications (
                application_id, submission_id, workflow_id, initial_run_id,
                submission_bucket, submission_key, submission_sha256,
                status, processing_started_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'PROCESSING', %s)
            ON CONFLICT (application_id) DO NOTHING
            """,
            (inp.application_id, inp.submission_id, inp.workflow_id, inp.initial_run_id,
             inp.submission_ref.bucket, inp.submission_ref.key, inp.submission_ref.sha256,
             inp.processing_started_at))
    except (UniqueViolation, CheckViolation) as exc:
        raise integrity(f"registration conflicts with an existing record: {type(exc).__name__}")
    row = await lock_application(cur, inp.application_id)
    # On retry (or a re-delivered start) verify identity; never reset state.
    if (str(row["submission_id"]) != inp.submission_id
            or row["submission_bucket"] != inp.submission_ref.bucket
            or row["submission_key"] != inp.submission_ref.key
            or row["submission_sha256"] != inp.submission_ref.sha256
            or row["workflow_id"] != inp.workflow_id):
        raise integrity("application already registered with a different submission identity")
    await insert_events(cur, inp.application_id, inp.workflow_id, inp.run_id, [inp.event])
    return {"application_id": inp.application_id, "status": row["status"]}


# ---- app_record_event ---------------------------------------------------------


async def record_event(cur: AsyncCursor, inp: RecordEventInput) -> dict[str, Any]:
    app = await lock_application(cur, inp.application_id)
    fields = dict(inp.projection.set_fields)
    if "status" in fields and fields["status"] is not None:
        check_status_transition(app["status"], fields["status"])

    ms = inp.milestone
    if ms is not None:
        await cur.execute(
            "SELECT status FROM kyc.review_tasks WHERE application_id = %s AND review_task_id = %s FOR UPDATE",
            (inp.application_id, ms.review_task_id))
        task = await cur.fetchone()
        if task is None:
            raise integrity("milestone references an unknown review task")
        status = task["status"]
        if ms.kind == "REMINDER":
            if status in OPEN_REVIEW_STATUSES:
                await cur.execute(
                    "UPDATE kyc.review_tasks SET reminded_at = COALESCE(reminded_at, %s) "
                    "WHERE application_id = %s AND review_task_id = %s",
                    (ms.at, inp.application_id, ms.review_task_id))
        elif ms.kind == "ESCALATION":
            if status in OPEN_REVIEW_STATUSES:
                await cur.execute(
                    "UPDATE kyc.review_tasks SET status = 'ESCALATED', "
                    "escalated_at = COALESCE(escalated_at, %s), "
                    "assigned_to = COALESCE(%s, assigned_to) "
                    "WHERE application_id = %s AND review_task_id = %s",
                    (ms.at, ms.assigned_queue, inp.application_id, ms.review_task_id))
        elif ms.kind == "EXPIRY":
            if status in OPEN_REVIEW_STATUSES:
                await cur.execute(
                    "UPDATE kyc.review_tasks SET status = 'EXPIRED', closed_at = %s "
                    "WHERE application_id = %s AND review_task_id = %s",
                    (ms.at, inp.application_id, ms.review_task_id))
            elif status != "EXPIRED":
                raise integrity(f"review task is {status}; cannot expire")
            check_status_transition(app["status"], "REVIEW_EXPIRED")
            fields.update({"status": "REVIEW_EXPIRED", "completed_at": ms.at,
                           "waiting_reason": None, "current_stage": "REVIEW_EXPIRED"})
        else:
            raise integrity(f"unknown milestone kind {ms.kind}")

    await _update_application(cur, inp.application_id, fields)
    await insert_events(cur, inp.application_id, inp.workflow_id, inp.run_id, inp.events)
    return {"application_id": inp.application_id}


# ---- 5.7 app_persist_assessment ----------------------------------------------


async def persist_assessment(cur: AsyncCursor, inp: PersistAssessmentInput) -> AssessmentPersisted:
    app = await lock_application(cur, inp.application_id)
    check_status_transition(app["status"], inp.target_status)
    if app["status"] in POST_APPROVAL_STATUSES:
        raise integrity("assessment write after final decision")

    try:
        await _persist_assessment_rows(cur, inp)
    except (UniqueViolation, ForeignKeyViolation, CheckViolation) as exc:
        raise integrity(f"assessment conflicts with stored records: {type(exc).__name__}: {exc.diag.constraint_name}")

    error_fields = (inp.error_code, inp.error_message) if inp.target_status == "PROCESSING_ERROR" else (None, None)
    await cur.execute(
        """
        UPDATE kyc.applications SET
            status = %s,
            stage_started_at = CASE WHEN current_stage <> %s THEN now() ELSE stage_started_at END,
            current_stage = %s,
            waiting_reason = %s,
            review_deadline_at = COALESCE(%s, review_deadline_at),
            last_error_code = %s,
            last_error_message = %s,
            updated_at = now()
        WHERE application_id = %s
        """,
        (inp.target_status, inp.current_stage, inp.current_stage, inp.waiting_reason,
         inp.review_deadline_at, error_fields[0], error_fields[1], inp.application_id))
    await insert_events(cur, inp.application_id, inp.workflow_id, inp.run_id, inp.events)
    return AssessmentPersisted(
        application_id=inp.application_id,
        status=inp.target_status,
        evaluation_id=inp.evaluation.evaluation_id if inp.evaluation else None,
        review_task_id=inp.review.review_task_id if inp.review else None)


async def _persist_assessment_rows(cur: AsyncCursor, inp: PersistAssessmentInput) -> None:
    app_id = inp.application_id

    if inp.loaded is not None:
        ld = inp.loaded
        await cur.execute(
            """
            UPDATE kyc.applications SET
                declared_applicant = COALESCE(declared_applicant, %s),
                submitted_at = COALESCE(submitted_at, %s),
                client_reference = COALESCE(client_reference, %s),
                updated_at = now()
            WHERE application_id = %s
            """,
            (Jsonb(ld.declared_applicant), ld.submitted_at, ld.client_reference, app_id))

    outcomes = {o.document_id: o for o in inp.document_outcomes}
    for ref in inp.documents:
        o = outcomes.get(ref.document_id)
        status = o.status if o else "NOT_RUN"
        await cur.execute(
            """
            INSERT INTO kyc.documents (
                application_id, document_id, client_document_reference, document_type,
                original_filename, object_bucket, object_key, size_bytes, sha256,
                extraction_status, extracted_fields, error_code, error_message,
                activity_attempt, extracted_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (application_id, document_id) DO UPDATE SET
                extraction_status = EXCLUDED.extraction_status,
                extracted_fields = EXCLUDED.extracted_fields,
                error_code = EXCLUDED.error_code,
                error_message = EXCLUDED.error_message,
                activity_attempt = EXCLUDED.activity_attempt,
                extracted_at = EXCLUDED.extracted_at,
                updated_at = now()
            WHERE kyc.documents.sha256 = EXCLUDED.sha256
              AND kyc.documents.object_key = EXCLUDED.object_key
              AND NOT (kyc.documents.extraction_status = 'SUCCEEDED' AND EXCLUDED.extraction_status <> 'SUCCEEDED')
            """,
            (app_id, ref.document_id, ref.client_document_reference, ref.document_type,
             ref.original_filename or "unknown", ref.bucket, ref.object_key, ref.size_bytes, ref.sha256,
             status, Jsonb(o.extracted_fields) if o and o.extracted_fields is not None else None,
             o.error_code if o else None, o.error_message if o else None,
             o.attempts if o else 0, o.extracted_at if o else None))

    sc = inp.screening
    if sc is not None:
        await cur.execute(
            """
            INSERT INTO kyc.screenings (
                application_id, screening_status, potential_ofac_match, pep_flag,
                result_details, provider_reference, error_code, error_message,
                activity_attempt, screened_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (application_id) DO UPDATE SET
                screening_status = EXCLUDED.screening_status,
                potential_ofac_match = EXCLUDED.potential_ofac_match,
                pep_flag = EXCLUDED.pep_flag,
                result_details = EXCLUDED.result_details,
                provider_reference = EXCLUDED.provider_reference,
                error_code = EXCLUDED.error_code,
                error_message = EXCLUDED.error_message,
                activity_attempt = EXCLUDED.activity_attempt,
                screened_at = EXCLUDED.screened_at,
                updated_at = now()
            WHERE kyc.screenings.screening_status <> 'SUCCEEDED'
            """,
            (app_id, sc.status, sc.potential_ofac_match, sc.pep_flag,
             Jsonb(sc.result_details) if sc.result_details is not None else None,
             sc.provider_reference, sc.error_code, sc.error_message, sc.attempts, sc.screened_at))

    ev = inp.evaluation
    if ev is not None:
        if ev.policy is None:
            raise integrity("evaluation is missing its policy snapshot")
        p = ev.policy
        await cur.execute(
            """
            INSERT INTO kyc.evaluations (
                application_id, evaluation_id, evaluation_sequence, policy_id, policy_version,
                policy_bucket, policy_key, policy_sha256, policy_snapshot, policy_resolved_at,
                evaluated_facts, rule_results, evaluation_outcome, evaluated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (application_id, evaluation_id) DO NOTHING
            """,
            (app_id, ev.evaluation_id, ev.evaluation_sequence, p.policy_id, p.version, p.bucket,
             p.object_key, p.sha256, Jsonb(p.policy), p.resolved_at, Jsonb(ev.evaluated_facts),
             Jsonb(ev.rule_results), ev.evaluation_outcome, ev.evaluated_at))
        if cur.rowcount == 0:
            await cur.execute(
                "SELECT policy_sha256, evaluation_sequence FROM kyc.evaluations "
                "WHERE application_id = %s AND evaluation_id = %s", (app_id, ev.evaluation_id))
            stored = await cur.fetchone()
            if stored["policy_sha256"] != p.sha256 or stored["evaluation_sequence"] != ev.evaluation_sequence:
                raise integrity("evaluation already stored with different immutable inputs")
        for f in ev.findings:
            await cur.execute(
                """
                INSERT INTO kyc.findings (
                    application_id, evaluation_id, finding_key, document_id, source,
                    finding_type, rule_id, severity, description, evidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (application_id, evaluation_id, finding_key) DO NOTHING
                """,
                (app_id, ev.evaluation_id, f.finding_key, f.document_id, f.source, f.finding_type,
                 f.rule_id, f.severity, f.description, Jsonb(f.evidence)))

    rv = inp.review
    if rv is not None:
        await cur.execute(
            """
            INSERT INTO kyc.review_tasks (
                application_id, review_task_id, evaluation_id, review_key, reason, status,
                assigned_to, reminder_due_at, escalation_due_at, expires_at,
                reminded_at, escalated_at, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (application_id, review_task_id) DO NOTHING
            """,
            (app_id, rv.review_task_id, rv.evaluation_id, rv.review_key, rv.reason, rv.status,
             rv.assigned_to, rv.reminder_due_at, rv.escalation_due_at, rv.expires_at,
             rv.reminded_at, rv.escalated_at, rv.opened_at))

    if inp.supersede_review_task_id:
        await cur.execute(
            "UPDATE kyc.review_tasks SET status = 'SUPERSEDED', closed_at = now() "
            "WHERE application_id = %s AND review_task_id = %s AND status = ANY(%s)",
            (app_id, inp.supersede_review_task_id, list(OPEN_REVIEW_STATUSES)))


# ---- 5.8 app_persist_final_outcome --------------------------------------------


async def persist_final_outcome(cur: AsyncCursor, inp: FinalOutcomeInput) -> FinalOutcomePersisted:
    app = await lock_application(cur, inp.application_id)

    await cur.execute("SELECT * FROM kyc.decisions WHERE application_id = %s", (inp.application_id,))
    existing = await cur.fetchone()
    if existing is not None:
        same = (existing["decision"] == inp.decision
                and str(existing["evaluation_id"]) == inp.evaluation_id
                and (str(existing["review_task_id"]) if existing["review_task_id"] else None) == inp.review_task_id)
        if not same:
            raise integrity("a contradictory decision already exists")
        await insert_events(cur, inp.application_id, inp.workflow_id, inp.run_id, inp.events)
        return FinalOutcomePersisted(inp.application_id, existing["decision"], app["status"])

    if app["status"] not in ("PROCESSING", "AWAITING_REVIEW"):
        raise integrity(f"cannot record a decision while application is {app['status']}")

    await cur.execute(
        "SELECT 1 FROM kyc.evaluations WHERE application_id = %s AND evaluation_id = %s",
        (inp.application_id, inp.evaluation_id))
    if await cur.fetchone() is None:
        raise integrity("decision references an unknown evaluation")

    if inp.decision_source == "HUMAN":
        await cur.execute(
            "SELECT evaluation_id, status FROM kyc.review_tasks "
            "WHERE application_id = %s AND review_task_id = %s FOR UPDATE",
            (inp.application_id, inp.review_task_id))
        task = await cur.fetchone()
        if task is None or str(task["evaluation_id"]) != inp.evaluation_id:
            raise integrity("review task does not belong to the decided evaluation")
        if task["status"] not in OPEN_REVIEW_STATUSES:
            raise integrity(f"review task is {task['status']}; it cannot accept a decision")
        resolution = "APPROVE" if inp.decision == "APPROVED" else "REJECT"
        await cur.execute(
            """
            UPDATE kyc.review_tasks SET status = 'COMPLETED', review_request_id = %s,
                resolution = %s, resolution_notes = %s, resolved_by = %s,
                completed_at = %s, closed_at = %s
            WHERE application_id = %s AND review_task_id = %s
            """,
            (inp.review_request_id, resolution, inp.resolution_notes, inp.decided_by,
             inp.decided_at, inp.decided_at, inp.application_id, inp.review_task_id))

    try:
        await cur.execute(
            """
            INSERT INTO kyc.decisions (
                application_id, evaluation_id, review_task_id, decision, decision_source,
                reason_code, reason_details, decided_by, decided_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (inp.application_id, inp.evaluation_id, inp.review_task_id, inp.decision,
             inp.decision_source, inp.reason_code, inp.reason_details, inp.decided_by, inp.decided_at))
    except (UniqueViolation, ForeignKeyViolation, CheckViolation) as exc:
        raise integrity(f"decision rejected by database: {type(exc).__name__}")

    if inp.decision == "REJECTED":
        new_status, stage, completed = "REJECTED", "COMPLETE", inp.decided_at
    else:
        new_status, stage, completed = "APPROVED", "KYC_APPROVED", None
        if not inp.activation_idempotency_key:
            raise integrity("an approved decision requires an activation idempotency key")
        await cur.execute(
            """
            INSERT INTO kyc.account_activations (
                application_id, idempotency_key, status, requested_at)
            VALUES (%s, %s, 'PENDING', %s)
            ON CONFLICT (application_id) DO NOTHING
            """,
            (inp.application_id, inp.activation_idempotency_key, inp.decided_at))

    check_status_transition(app["status"], new_status)
    await cur.execute(
        """
        UPDATE kyc.applications SET status = %s, current_stage = %s, waiting_reason = NULL,
            review_deadline_at = NULL, stage_started_at = now(), completed_at = %s,
            updated_at = now()
        WHERE application_id = %s
        """,
        (new_status, stage, completed, inp.application_id))
    await insert_events(cur, inp.application_id, inp.workflow_id, inp.run_id, inp.events)
    return FinalOutcomePersisted(inp.application_id, inp.decision, new_status)


# ---- 5.10 app_persist_activation -------------------------------------------------


async def persist_activation(cur: AsyncCursor, inp: PersistActivationInput) -> dict[str, Any]:
    app = await lock_application(cur, inp.application_id)
    await cur.execute(
        "SELECT a.*, d.decision FROM kyc.account_activations a "
        "JOIN kyc.decisions d USING (application_id) WHERE a.application_id = %s FOR UPDATE OF a",
        (inp.application_id,))
    act = await cur.fetchone()
    if act is None or act["decision"] != "APPROVED":
        raise integrity("activation requires an APPROVED decision and a registered activation request")

    if inp.result == "ACTIVE":
        rc = inp.receipt
        if rc is None:
            raise integrity("ACTIVE result requires a provider receipt")
        if act["status"] == "ACTIVE" and act["account_id"] != rc.account_id:
            raise integrity("activation already confirmed with a different account")
        check_status_transition(app["status"], "ACTIVE")
        try:
            await cur.execute(
                """
                UPDATE kyc.account_activations SET status = 'ACTIVE', account_id = %s,
                    provider_reference = %s, activated_at = %s, confirmed_at = %s,
                    error_code = NULL, updated_at = now()
                WHERE application_id = %s
                """,
                (rc.account_id, rc.provider_reference, rc.activated_at, rc.confirmed_at, inp.application_id))
        except UniqueViolation:
            raise integrity("account id already belongs to another application")
        await cur.execute(
            """
            UPDATE kyc.applications SET status = 'ACTIVE', current_stage = 'COMPLETE',
                waiting_reason = NULL, activation_deadline_at = NULL, last_error_code = NULL,
                last_error_message = NULL, stage_started_at = now(), completed_at = %s,
                updated_at = now()
            WHERE application_id = %s
            """,
            (rc.confirmed_at, inp.application_id))
    elif inp.result in ("TECHNICAL_ERROR", "BLOCKED"):
        if act["status"] == "ACTIVE":
            raise integrity("activation is already confirmed ACTIVE")
        check_status_transition(app["status"], "ACTIVATION_ERROR")
        await cur.execute(
            "UPDATE kyc.account_activations SET status = %s, error_code = %s, updated_at = now() "
            "WHERE application_id = %s", (inp.result, inp.error_code, inp.application_id))
        await cur.execute(
            """
            UPDATE kyc.applications SET status = 'ACTIVATION_ERROR', current_stage = 'ACTIVATION',
                waiting_reason = 'ACTIVATION_RECOVERY', last_error_code = %s, last_error_message = %s,
                stage_started_at = now(), updated_at = now()
            WHERE application_id = %s
            """,
            (inp.error_code, inp.error_message, inp.application_id))
    else:
        raise integrity(f"unknown activation result {inp.result}")

    await insert_events(cur, inp.application_id, inp.workflow_id, inp.run_id, inp.events)
    return {"application_id": inp.application_id, "result": inp.result}
