# KYC Demo — Temporal Workflow and Postgres Specification


Version: 1.1
Date: 20 October 2026


## 1. Scope


Process synthetic KYC applications submitted through the Intake API.


The design demonstrates:
- Durable application processing.
- Document fan-out/fan-in.
- Just-in-time document retrieval.
- Separate OCR and screening stubs.
- Retry-safe database writes.
- Human review with durable reminder, escalation, and expiry timers.
- Append-only business audit events and operator visibility.
- Account activation after approval.
- Policy changes affecting applications already in flight.


Credit-history rules are supplemental onboarding eligibility rules used for the policy demonstration; they are not identity-verification requirements.


## 2. Ownership boundaries


| Component | Responsibility |
|---|---|
| Intake API | Accept applications and documents; freeze submissions; start workflows |
| Garage | Store submitted packages, document bytes, and versioned policy files |
| Temporal workflow | Coordinate Activities, retries, concurrency, decisions, and review waits |
| Activities | Perform storage access, service calls, evaluation, and database writes |
| OCR stub | Return synthetic extraction outcomes |
| Screening stub | Return synthetic OFAC/PEP screening outcomes |
| Postgres | Store processing records, assessments, findings, reviews, activation receipts, and audit events |
| Review/operator API | Authenticate reviewers/operators and deliver validated Temporal Updates |
| Account service stub | Idempotently activate an approved applicant's account |


Intake does not access the business Postgres database.
Temporal's internal persistence is separate from these application tables.


## 3. Workflow identity and input


Workflow type: KycApplicationWorkflow
Task queue: kyc-applications
Workflow ID: kyc-{application_id}


~~~json
{
  "schema_version": "1.0",
  "application_id": "11111111-1111-4111-8111-111111111111",
  "submission_id": "33333333-3333-4333-8333-333333333333",
  "submission_ref": {
    "bucket": "kyc-demo",
    "key": "applications/11111111-1111-4111-8111-111111111111/submission.json",
    "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  }
}
~~~


No document bytes are included.


The workflow derives stable Activity IDs and evaluation identifiers before scheduling work. Identifiers must remain unchanged across Activity retries. An optional timer_profile input selects a versioned normal-v1 or demo-v1 configuration. Default to normal-v1; capture the profile in workflow input and do not vary its constants across replay-compatible code versions.


## 4. Activity sequence


| Order | Activity | Responsibility |
|---|---|---|
| 1 | app_start_processing | Create the minimal Postgres application record |
| 2 | app_load_submission | Retrieve and validate the frozen submission manifest |
| 3 | app_document_parse | Retrieve and parse one document; bounded parallel execution |
| 4 | app_load_policy | Resolve the current policy and return its immutable snapshot |
| 5 | app_evaluate_application | Evaluate declarations and extracted facts using that snapshot |
| 6 | app_screen_applicant | Call the screening stub |
| 7 | app_persist_assessment | Persist processing results, evaluation, findings, and any review task |
| 8 | app_persist_final_outcome | Persist the final automatic or human KYC decision |
| 9 | app_activate_account | Approved branch only: invoke the account service with a stable idempotency key |
| 10 | app_persist_activation | Persist the confirmed account activation receipt and ACTIVE status |


Human review, when required, occurs between steps 7 and 8. Rejected and review-expired applications never enter activation. Approved is a KYC decision; ACTIVE is completed onboarding. app_record_event is a supporting persistence Activity invoked at state transitions, document outcomes, timer milestones, and accepted operator commands; it is not an extra business check.


app_load_policy is an explicit policy-resolution boundary. It keeps external policy access separate from evaluation and makes the selected policy visible in Temporal history.


## 5. Activity contracts


### 5.1 app_start_processing


Insert an application record containing:
- Application and submission IDs.
- Submission bucket, key, and hash.
- Workflow ID and initial run ID.
- Status PROCESSING.
- Processing start time.


Applicant details and submitted timestamp remain null until assessment persistence.


On retry:
- Reuse the existing record.
- Verify submission ID, reference, and hash.
- Do not reset status, timestamps, assessments, or decisions.
- Reject a conflicting identity as a non-retryable integrity error.


No subsequent processing begins until registration succeeds.


### 5.2 app_load_submission


Read only the frozen submission manifest from Garage.


Validate:
- Submission hash.
- Schema version.
- Application and submission IDs.
- Unique document IDs.
- Document references belonging to the application.


Return:
- Declared applicant information.
- Submission timestamp.
- Application client reference.
- Document metadata and storage references.


Do not retrieve document bodies or write Postgres.


A document reference includes:


~~~json
{
  "application_id": "11111111-1111-4111-8111-111111111111",
  "document_id": "22222222-2222-4222-8222-222222222222",
  "document_type": "passport",
  "bucket": "kyc-demo",
  "object_key": "applications/11111111-1111-4111-8111-111111111111/documents/22222222-2222-4222-8222-222222222222/content.pdf",
  "size_bytes": 4096,
  "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "client_document_reference": "ks1:morning-demo:a000001:d001:o=ok"
}
~~~


### 5.3 app_document_parse


Execute once per document.
1. Retrieve document bytes from Garage just in time.
2. Verify size and checksum.
3. Call the OCR stub.
4. Return compact extracted fields or a document error.
5. Release temporary resources in all exit paths.


| Result | Handling |
|---|---|
| Successful extraction | Return SUCCEEDED and extracted fields |
| Corrupt/unreadable document | Return DOCUMENT_ERROR; no technical retry |
| Service timeout/unavailability | Raise retryable Activity error |
| Reference or checksum integrity failure | Raise non-retryable Activity error |


After retries are exhausted, the workflow records a compact TECHNICAL_ERROR outcome for that document.


The Activity does not write Postgres.


### 5.4 app_load_policy


Resolve a configured policy ID through Garage's current-policy pointer, then retrieve its immutable version.


Return:
- Policy ID and version.
- Policy bucket and object key.
- Verified policy hash.
- Complete bounded policy JSON.
- Resolution timestamp.


Validate the pointer, hash, policy schema, supported operators, and unique rule IDs.


Do not silently fall back to an older policy when the current policy is invalid or unavailable.


The successful Activity result pins the policy for this evaluation. Evaluation retries receive the same policy snapshot.


If this loading Activity fails before returning a recorded result, a retry may observe a newer current version. The guarantee begins once Temporal records its successful result.


### 5.5 app_evaluate_application


Inputs:
- Evaluation ID.
- Declared applicant information.
- Document extraction outcomes.
- Pinned policy JSON and identity.


Responsibilities:
- Normalize facts according to explicit rules.
- Detect conflicting document facts.
- Evaluate required evidence and policy rules.
- Return rule results and findings.
- Identify whether evidence-related human review is required.


No Garage reads, service calls, or database writes.


A rule result includes:


~~~json
{
  "rule_id": "minimum_average_credit_age",
  "observed_value": 60,
  "operator": "gt",
  "threshold": 72,
  "outcome": "FAIL",
  "disposition": "REVIEW"
}
~~~


Possible rule outcomes: PASS, FAIL, MISSING, CONFLICT.
Missing or conflicting facts must not silently become passing values.


### 5.6 app_screen_applicant


Call the screening stub with:
- Application ID.
- Declared applicant information.
- Application client reference.
- Stable invocation identity.
- Activity attempt number.


Return:
- Screening outcome.
- Potential OFAC-match flag.
- PEP flag.
- Compact match details.
- Provider reference.


Potential matches and PEP flags are successful service responses containing business findings. They are not service failures or automatic rejection instructions.


Service errors and timeouts follow technical retry policy.


### 5.7 app_persist_assessment


In one Postgres transaction:
1. Populate loaded application details.
2. Insert/update document metadata and extraction outcomes.
3. Insert/update screening results.
4. Insert the evaluation, selected policy snapshot, and rule results.
5. Insert assessment findings.
6. Create a review task if required.
7. Update application status and error information.


Accept partial inputs for technical failure handling.


For example, submission-loading failure produces an application processing error without fabricating documents or an evaluation.


Return application ID, evaluation ID if available, application status, and review task ID if applicable.


### 5.8 app_persist_final_outcome


In one Postgres transaction:
- Insert the final decision.
- Update application status and completion time.
- Complete the associated review task when applicable.


A repeated identical request succeeds without duplication.
A contradictory existing decision is an integrity error, not an overwrite.
A rejected application completes after this transaction. An approved application proceeds to account activation; the workflow reports successful onboarding only after app_persist_activation succeeds.


## 6. Document concurrency and payload boundaries


Default parsing concurrency: four documents per application.


The workflow:
1. Orders references deterministically.
2. Schedules up to the concurrency limit.
3. Starts additional parses as slots become available.
4. Collects one terminal outcome per document.
5. Evaluates the application after fan-in completes.


Catch failures independently. One failed document must not discard successful results or automatically cancel remaining parses.


Configure worker-wide concurrency separately to limit total processing across applications.


Only references and compact results enter Temporal history. Document bytes, base64 documents, and large raw provider responses are prohibited.


## 7. Policy storage and changes


Garage layout:


| Object | Contents |
|---|---|
| policies/onboarding/current.json | Pointer to the current immutable policy version |
| policies/onboarding/versions/1.json | Immutable version 1 |
| policies/onboarding/versions/2.json | Immutable version 2 |


Current pointer:


~~~json
{
  "policy_id": "onboarding",
  "version": 2,
  "object_key": "policies/onboarding/versions/2.json",
  "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
}
~~~


Example policy:


~~~json
{
  "schema_version": "1.0",
  "policy_id": "onboarding",
  "version": 2,
  "rules": [
    {
      "rule_id": "minimum_average_credit_age",
      "field": "average_credit_age_months",
      "operator": "gt",
      "threshold": 72,
      "on_failure": "REVIEW",
      "on_missing": "REVIEW",
      "on_conflict": "REVIEW"
    }
  ]
}
~~~


Version 1 uses threshold 48; version 2 uses 72.


Publish a change by writing and verifying the immutable version first, then replacing the current pointer.
Do not modify an existing version in place.


### Mid-flight behavior


| Application state when policy changes | Behavior |
|---|---|
| Before successful policy resolution | Resolves the current version observed by the successful load attempt |
| Policy already resolved | Current evaluation and its retries use the pinned snapshot |
| Awaiting review | Retains its assessment unless an authorized request_policy_reevaluation Update is accepted |
| Re-evaluation accepted | Load the explicitly requested immutable version; evaluate cached facts under a new evaluation ID |
| Final KYC decision recorded or activation begun | Not re-evaluated in place; subsequent remediation is a separate process |


Changing current.json alone is not a broadcast to every workflow. The default is latest policy at the evaluation boundary, followed by pinning. Explicit re-evaluation of applications awaiting review is supported as described in section 17. Business policy JSON changes are distinct from workflow-code changes, which still require replay-compatible deployment.


### Demonstration


1. Evaluate application A with average credit age 60 months under v1: pass.
2. Start application B and keep it in document parsing.
3. Publish v2.
4. B resolves v2 after fan-in: review.
5. Compare stored policy versions, thresholds, facts, and outcomes.


To provide the fact, extend the synthetic document fixture contract with average_credit_age_months on an other document representing synthetic credit-profile evidence. The OCR stub returns that fixture field. This requires corresponding seeder/OCR fixture support; it is not inferred from passports or licenses.


## 8. Disposition and human review


Initial policy:


| Condition | Action |
|---|---|
| Complete evidence, passing rules, clear screening | Automatic approval |
| Document errors, missing/conflicting facts, failed review rules, OFAC/PEP concerns | Human review |
| Unresolved technical failure | Processing error |


The workflow owns disposition.


For review:
- Persist assessment and review task first.
- Wait durably for a Temporal Update.
- Validate application ID, review task ID, request ID, and decision.
- Persist the accepted final outcome.


The review API authenticates reviewers.


Duplicate review request IDs must not duplicate effects. Conflicting decisions are rejected.


No Activity remains running during review. Durable reminder, escalation, and expiry timers are specified in section 14. Review Update acceptance does not mean account activation has completed.


## 9. Postgres schema


Nine tables support the workflow, versioned assessments, activation, and business audit trail.


| Table | Purpose |
|---|---|
| applications | Current application processing state |
| documents | Document metadata and extraction results |
| screenings | Screening result |
| evaluations | Policy snapshot, evaluated facts, and rule results |
| findings | Assessment-specific issues |
| review_tasks | Human review requests |
| decisions | Final application decision |
| account_activations | Account-service activation receipt and outcome |
| audit_events | Append-only, idempotent business events |


~~~sql
CREATE SCHEMA IF NOT EXISTS kyc;


CREATE TABLE kyc.applications (
    application_id uuid PRIMARY KEY,
    submission_id uuid NOT NULL UNIQUE,
    client_reference varchar(128),
    workflow_id text NOT NULL UNIQUE,
    initial_run_id uuid NOT NULL,
    submission_bucket text NOT NULL,
    submission_key text NOT NULL,
    submission_sha256 text NOT NULL
        CHECK (submission_sha256 ~ '^[0-9a-f]{64}$'),
    -- Null until loaded details are persisted.
    declared_applicant jsonb
        CHECK (jsonb_typeof(declared_applicant) = 'object'),
    submitted_at timestamptz,
    status text NOT NULL DEFAULT 'PROCESSING'
        CHECK (status IN (
            'PROCESSING', 'AWAITING_REVIEW', 'APPROVED',
            'REJECTED', 'PROCESSING_ERROR', 'REVIEW_EXPIRED',
            'ACTIVATING', 'ACTIVE', 'ACTIVATION_ERROR'
        )),
    current_stage text NOT NULL DEFAULT 'REGISTERED',
    waiting_reason text,
    stage_started_at timestamptz NOT NULL DEFAULT now(),
    review_deadline_at timestamptz,
    activation_deadline_at timestamptz,
    last_error_code text,
    last_error_message text,
    processing_started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);


CREATE TABLE kyc.documents (
    application_id uuid NOT NULL
        REFERENCES kyc.applications(application_id),
    document_id uuid NOT NULL,
    client_document_reference varchar(128),
    document_type text NOT NULL,
    original_filename text NOT NULL,
    object_bucket text NOT NULL,
    object_key text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes > 0),
    sha256 text NOT NULL
        CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    extraction_status text NOT NULL
        CHECK (extraction_status IN (
            'PENDING', 'NOT_RUN', 'SUCCEEDED',
            'DOCUMENT_ERROR', 'TECHNICAL_ERROR'
        )),
    extracted_fields jsonb
        CHECK (jsonb_typeof(extracted_fields) = 'object'),
    error_code text,
    error_message text,
    activity_attempt integer NOT NULL DEFAULT 0
        CHECK (activity_attempt >= 0),
    extracted_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (application_id, document_id),
    CHECK (
        extraction_status <> 'SUCCEEDED'
        OR extracted_fields IS NOT NULL
    )
);


CREATE TABLE kyc.screenings (
    application_id uuid PRIMARY KEY
        REFERENCES kyc.applications(application_id),
    screening_status text NOT NULL
        CHECK (screening_status IN (
            'PENDING', 'NOT_RUN', 'SUCCEEDED', 'TECHNICAL_ERROR'
        )),
    potential_ofac_match boolean,
    pep_flag boolean,
    result_details jsonb,
    provider_reference text,
    error_code text,
    error_message text,
    activity_attempt integer NOT NULL DEFAULT 0
        CHECK (activity_attempt >= 0),
    screened_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        screening_status <> 'SUCCEEDED'
        OR (
            potential_ofac_match IS NOT NULL
            AND pep_flag IS NOT NULL
        )
    ),
    CHECK (
        screening_status = 'SUCCEEDED'
        OR (
            potential_ofac_match IS NULL
            AND pep_flag IS NULL
        )
    )
);


CREATE TABLE kyc.evaluations (
    application_id uuid NOT NULL
        REFERENCES kyc.applications(application_id),
    evaluation_id uuid NOT NULL,
    evaluation_sequence integer NOT NULL
        CHECK (evaluation_sequence > 0),
    policy_id text NOT NULL,
    policy_version integer NOT NULL CHECK (policy_version > 0),
    policy_bucket text NOT NULL,
    policy_key text NOT NULL,
    policy_sha256 text NOT NULL
        CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
    policy_snapshot jsonb NOT NULL
        CHECK (jsonb_typeof(policy_snapshot) = 'object'),
    policy_resolved_at timestamptz NOT NULL,
    -- Facts actually used, including provenance/conflict information.
    evaluated_facts jsonb NOT NULL
        CHECK (jsonb_typeof(evaluated_facts) = 'object'),
    rule_results jsonb NOT NULL
        CHECK (jsonb_typeof(rule_results) = 'array'),
    evaluation_outcome text NOT NULL
        CHECK (evaluation_outcome IN ('PASS', 'REVIEW')),
    evaluated_at timestamptz NOT NULL,
    persisted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (application_id, evaluation_id),
    UNIQUE (application_id, evaluation_sequence)
);


CREATE TABLE kyc.findings (
    application_id uuid NOT NULL,
    evaluation_id uuid NOT NULL,
    finding_key text NOT NULL,
    document_id uuid,
    source text NOT NULL
        CHECK (source IN ('DOCUMENT', 'POLICY', 'SCREENING')),
    finding_type text NOT NULL,
    rule_id text,
    severity text NOT NULL
        CHECK (severity IN ('INFO', 'WARNING', 'BLOCKING')),
    description text NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    disposition text NOT NULL DEFAULT 'OPEN'
        CHECK (disposition IN (
            'OPEN', 'ACCEPTED', 'DISMISSED', 'RESOLVED'
        )),
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    PRIMARY KEY (application_id, evaluation_id, finding_key),
    FOREIGN KEY (application_id, evaluation_id)
        REFERENCES kyc.evaluations(application_id, evaluation_id),
    FOREIGN KEY (application_id, document_id)
        REFERENCES kyc.documents(application_id, document_id)
);


CREATE TABLE kyc.review_tasks (
    application_id uuid NOT NULL,
    review_task_id uuid NOT NULL,
    evaluation_id uuid NOT NULL,
    review_key text NOT NULL,
    reason text NOT NULL,
    status text NOT NULL DEFAULT 'OPEN'
        CHECK (status IN (
            'OPEN', 'ASSIGNED', 'ESCALATED', 'COMPLETED',
            'CANCELLED', 'EXPIRED', 'SUPERSEDED'
        )),
    assigned_to text,
    reminder_due_at timestamptz NOT NULL,
    escalation_due_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    reminded_at timestamptz,
    escalated_at timestamptz,
    closed_at timestamptz,
    review_request_id uuid,
    resolution text
        CHECK (resolution IN ('APPROVE', 'REJECT')),
    resolution_notes text,
    resolved_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CHECK (created_at <= reminder_due_at
        AND reminder_due_at < escalation_due_at
        AND escalation_due_at < expires_at),
    PRIMARY KEY (application_id, review_task_id),
    UNIQUE (application_id, evaluation_id, review_key),
    UNIQUE (review_request_id),
    FOREIGN KEY (application_id, evaluation_id)
        REFERENCES kyc.evaluations(application_id, evaluation_id),
    CHECK (
        status <> 'COMPLETED'
        OR (
            review_request_id IS NOT NULL
            AND resolution IS NOT NULL
            AND resolved_by IS NOT NULL
            AND completed_at IS NOT NULL
        )
    )
);


CREATE TABLE kyc.decisions (
    application_id uuid PRIMARY KEY,
    evaluation_id uuid NOT NULL,
    review_task_id uuid,
    decision text NOT NULL
        CHECK (decision IN ('APPROVED', 'REJECTED')),
    decision_source text NOT NULL
        CHECK (decision_source IN ('AUTOMATIC', 'HUMAN')),
    reason_code text NOT NULL,
    reason_details text,
    decided_by text NOT NULL,
    decided_at timestamptz NOT NULL,
    FOREIGN KEY (application_id, evaluation_id)
        REFERENCES kyc.evaluations(application_id, evaluation_id),
    FOREIGN KEY (application_id, review_task_id)
        REFERENCES kyc.review_tasks(application_id, review_task_id),
    CHECK (
        (decision_source = 'AUTOMATIC' AND review_task_id IS NULL)
        OR
        (decision_source = 'HUMAN' AND review_task_id IS NOT NULL)
    )
);


CREATE INDEX applications_status_started_idx
    ON kyc.applications (status, processing_started_at);


CREATE INDEX applications_client_reference_idx
    ON kyc.applications (client_reference);


CREATE INDEX review_tasks_queue_idx
    ON kyc.review_tasks (status, created_at);


CREATE INDEX findings_open_idx
    ON kyc.findings (application_id, evaluation_id)
    WHERE disposition = 'OPEN';


CREATE INDEX evaluations_policy_idx
    ON kyc.evaluations (policy_id, policy_version);


CREATE TABLE kyc.account_activations (
    application_id uuid PRIMARY KEY
        REFERENCES kyc.decisions(application_id),
    idempotency_key text NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN (
        'PENDING', 'ACTIVE', 'TECHNICAL_ERROR', 'BLOCKED'
    )),
    account_id text UNIQUE,
    provider_reference text,
    requested_at timestamptz NOT NULL,
    activated_at timestamptz,
    confirmed_at timestamptz,
    error_code text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status <> 'ACTIVE'
        OR (account_id IS NOT NULL AND activated_at IS NOT NULL))
);


CREATE TABLE kyc.audit_events (
    application_id uuid NOT NULL
        REFERENCES kyc.applications(application_id),
    event_key text NOT NULL,
    event_sequence bigint NOT NULL CHECK (event_sequence > 0),
    event_type text NOT NULL,
    workflow_id text NOT NULL,
    run_id uuid NOT NULL,
    activity_id text,
    request_id text,
    actor_type text NOT NULL CHECK (actor_type IN (
        'WORKFLOW', 'REVIEWER', 'OPERATOR'
    )),
    actor_id text NOT NULL,
    reason text,
    occurred_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    details jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(details) = 'object'),
    PRIMARY KEY (application_id, event_key),
    UNIQUE (application_id, run_id, event_sequence)
);


CREATE INDEX review_tasks_expiry_idx
    ON kyc.review_tasks (expires_at)
    WHERE status IN ('OPEN', 'ASSIGNED', 'ESCALATED');


CREATE INDEX applications_stage_age_idx
    ON kyc.applications (current_stage, stage_started_at);


CREATE INDEX audit_events_timeline_idx
    ON kyc.audit_events (application_id, occurred_at);


CREATE INDEX account_activations_status_idx
    ON kyc.account_activations (status, requested_at);


-- Deploy with a separate migration owner.
-- Business runtime roles receive INSERT/SELECT on audit_events,
-- but no UPDATE/DELETE/TRUNCATE permissions.
~~~


## 10. Database write rules


### Registration


app_start_processing inserts only the minimal application row. It does not insert document or screening rows before the package is loaded.


### Assessment persistence


app_persist_assessment atomically writes the available results.


On technical failure before evaluation:
- Persist available application, document, and screening information.
- Set application status to PROCESSING_ERROR.
- Do not create a fabricated evaluation, finding, review task, or decision.


After successful evaluation:
- Store the exact policy snapshot.
- Store facts and rule results.
- Associate findings and reviews with that evaluation.
- Set AWAITING_REVIEW when appropriate.
- Otherwise retain PROCESSING until final decision persistence.


evaluations.evaluation_outcome describes document/policy evaluation. Screening can independently require review even when this outcome is PASS.


### Idempotency


| Record | Retry identity |
|---|---|
| Application | Application ID |
| Document | Application ID + document ID |
| Screening | Application ID |
| Evaluation | Application ID + evaluation ID |
| Finding | Application ID + evaluation ID + finding key |
| Review task | Application ID + evaluation ID + review key |
| Final decision | Application ID |
| Account activation | Application ID + stable provider idempotency key |
| Audit event | Application ID + event key |


Retries verify matching immutable inputs. Do not overwrite conflicting evaluations or decisions.


Lock the application row during assessment/finalization transactions to serialize writes. Refuse stale assessment writes that would regress finalized status.


Set updated_at explicitly on updates. Every business mutation must append its audit event in the same database transaction. Retry paths verify an existing event has matching immutable details.


## 11. Retry and failure policy


Proposed initial settings:


| Activity | Start-to-close | Maximum attempts |
|---|---|---|
| app_start_processing | 10 seconds | 5 |
| app_load_submission | 15 seconds | 5 |
| app_document_parse | 60 seconds | 5 |
| app_load_policy | 15 seconds | 5 |
| app_evaluate_application | 10 seconds | 3 |
| app_screen_applicant | 30 seconds | 5 |
| app_persist_assessment | 10 seconds | 5 |
| app_persist_final_outcome | 10 seconds | 5 |
| app_activate_account | 30 seconds | 5 |
| app_persist_activation | 10 seconds | 5 |
| app_record_event | 10 seconds | 5 |


Technical retries use exponential backoff: initial one second, coefficient two, maximum interval 30 seconds.


HTTP timeouts must leave sufficient time inside the Activity timeout for storage access and cleanup.


For fail-first-two stub scenarios:
- Forward stable invocation identity and Temporal attempt number.
- Attempts one and two fail.
- Attempt three succeeds.
- Duplicate requests for the same attempt do not advance the scenario.


Failure behavior:
- Collect all document outcomes before proceeding.
- If document processing has unresolved technical failures, persist partial results and wait for operator recovery; evaluation/screening are not run until recovery succeeds.
- Permanent document errors proceed to evaluation and screening as business evidence issues.
- Other exhausted stage failures persist available partial results and enter a durable operator-recovery wait as specified in section 16.
- Mark known, skipped processing stages NOT_RUN.
- Do not mark an infrastructure failure as applicant rejection.


If Postgres remains unavailable, persistence cannot be guaranteed. Surface the failure in Temporal and preserve completed Activity results for operator recovery.


## 12. Dashboard behavior


Postgres supports:
- Processing applications.
- Applications awaiting review.
- Approved/rejected counts.
- Technical processing failures.
- Findings by type.
- Assessments by policy version.
- Comparison of observed facts against policy thresholds.


Registration provides early application visibility. Detailed document, evaluation, and screening results appear when assessment persistence completes.


Temporal provides execution-level visibility before then, including running Activities and retry history.


This schema does not attempt to mirror Temporal history. Section 16 adds the live workflow Query, Search Attributes, operator actions, and alerts. Business events and current-stage projections are persisted progressively even though full assessment results are written later.


## 13. Acceptance criteria


1. Intake starts the workflow using a submission reference.
2. The first Activity registers a minimal application row.
3. Registration retries preserve the same row without resetting state.
4. Submission loading validates the package without loading document bodies.
5. Parsing retrieves bytes just in time and respects concurrency bounds.
6. Fan-in preserves successful results when another document fails.
7. Document bytes never enter Temporal history.
8. Policy v2 can affect an application already parsing under v1.
9. Evaluation retries use the policy snapshot returned by the completed policy-loading Activity.
10. Existing completed assessments do not silently change after publication.
11. Postgres retains policy identity, snapshot, facts, thresholds, and outcomes.
12. Technical failures remain distinct from business findings.
13. Partial failures persist without fabricated evaluations.
14. Review waits survive worker restart.
15. Duplicate persistence calls and review messages do not duplicate effects.
16. Final application status and decision commit atomically.
17. Intake remains independent of business Postgres.
18. SQL migrations and workflow integration tests must pass before implementation acceptance.
19. Review reminders, escalations, and expiry survive worker downtime without resetting deadlines.
20. Account activation is executed only after approval, and lost responses never cause duplicate accounts.
21. Operator queries identify the stage, wait reason, failed documents, review deadline, pinned policy, and activation state.
22. Each review decision, timer milestone, policy change request, activation, and operator command has a correlated audit event.
23. An awaiting-review application can be explicitly re-evaluated under v2, while v1 evidence remains immutable.
24. Race tests prevent stale reviewer decisions, late approvals, duplicate timer effects, and policy changes from authorizing activation.




## 14. Durable timers and review lifecycle


Use Temporal workflow timers (workflow.sleep / workflow.wait_condition with a timeout) and workflow time. Do not implement business deadlines with time.sleep, a polling Activity, a cron job, or a Postgres scheduler.


Activity timeouts bound individual calls; business timers bound customer/operator waits. They are separate mechanisms.


| Timer | Normal configuration | Accelerated demo | Behavior |
|---|---|---|---|
| Review reminder | 2 hours after review opens | 15 seconds | Flag the queue item and append REVIEW_REMINDER_DUE |
| Review escalation | 8 hours after review opens | 30 seconds | Escalate to the configured senior-compliance queue and append REVIEW_ESCALATED |
| Review expiry | 24 hours after review opens | 60 seconds | Close review as EXPIRED, application REVIEW_EXPIRED; end without a decision or account activation |
| Operator-recovery expiry | 7 days after recovery wait opens | 120 seconds | Append recovery-expired event and end unsuccessfully, preserving technical failure |
| Activation retry budget | 1 hour per activation scheduling generation | 60 seconds | Bound retries with Schedule-To-Close; then enter activation-error recovery wait |


Review deadlines are proposed demo business choices, not regulatory requirements. Freeze a versioned timer configuration in workflow input or a completed configuration-loading Activity; do not read mutable worker environment settings directly during workflow replay.


When opening review, the workflow computes opened_at, reminder_due_at, escalation_due_at, and expires_at using workflow time. Pass those exact values to app_persist_assessment and store them in review_tasks. Its created_at must use the supplied opened_at, not database insertion time. Do not reset the deadlines after worker restart, duplicate Update, or policy re-evaluation.


Reminder/escalation effects use app_record_event to atomically append the event and update the review-task flags/status. For v1 these are queue indicators, not email or SMS sends. A notification service may be added later using its own idempotent Activity.


If the worker is stopped, no Activity is held open for the review wait. On recovery, check the absolute expiry first. If already expired, record expiry without sending stale reminders. If escalation is overdue but expiry is not, apply escalation and record earlier missed milestones as historical facts without generating duplicate notifications.


Serialize state-changing Update handling and timer handling. Before accepting a reviewer decision, check the current task/evaluation and workflow time against expires_at. A request received by an API is not a completed review until the workflow processes and accepts it. If it is processed after expiry, reject it; do not trust a client timestamp. After a valid decision, ignore/cancel remaining review timers and do not allow escalation to reverse that decision.


The review API returns the accepted decision state separately from subsequent activation status. Await completion of accepted state-changing handlers before allowing the workflow to exit.


## 15. Business audit trail


Maintain two complementary records:


1. Temporal execution history: scheduled Activities, recorded results, timers, and workflow messages for durable execution and technical diagnosis.
2. Postgres audit_events: a queryable business timeline retained according to the application's audit retention requirements.


Temporal history is not a permanent compliance archive. The Postgres trail is application-append-only, not a cryptographically tamper-proof or WORM record.


Required business event types:
- PROCESSING_STARTED, STAGE_STARTED, STAGE_COMPLETED, DOCUMENT_PARSED, DOCUMENT_ERROR.
- POLICY_RESOLVED, EVALUATION_COMPLETED, ASSESSMENT_PERSISTED.
- REVIEW_OPENED, REVIEW_REMINDER_DUE, REVIEW_ESCALATED, REVIEW_EXPIRED.
- REVIEW_DECISION_ACCEPTED, POLICY_REEVALUATION_REQUESTED, REVIEW_SUPERSEDED.
- PROCESSING_ERROR, OPERATOR_RETRY_REQUESTED, RECOVERY_WAIT_EXPIRED.
- KYC_APPROVED, KYC_REJECTED, ACTIVATION_REQUESTED, ACTIVATION_CONFIRMED, ACTIVATION_ERROR.


Events carry application ID, workflow/run IDs, a stable event key, workflow-assigned sequence, occurrence and recording timestamps, actor, request ID where relevant, reason, and bounded structured details. Document events include document ID. Policy/evaluation events include evaluation ID, version/hash, and relevant rule/result identifiers.


Generate occurrence times and event identifiers once in workflow state and pass them into Activities. Never use a fresh random key on an Activity retry. Sequence is scoped to a workflow run; occurrence order and DB recording order may differ. Database retries must compare immutable event identity/details before treating a conflict as success.


Business persistence Activities insert their events in the same transaction as the business mutation. app_start_processing inserts the application and PROCESSING_STARTED together. app_persist_assessment, app_persist_final_outcome, and app_persist_activation append their corresponding events atomically.


app_record_event performs one database transition operation: append an event and update its related status projection, such as current_stage, waiting_reason, or review milestone. It makes no provider calls. The workflow invokes it before a stage starts and after results are collected; per-document outcome events may be batched by this persistence Activity with stable per-document keys. Complete the transition persistence before progressing to its next business side effect.


If Postgres is unavailable, the workflow cannot truthfully claim its business event was recorded. Block progression at required audit boundaries, retry, and surface the problem through Temporal visibility. Do not silently bypass audit persistence to activate an account.


Keep document bytes, credentials, unnecessary applicant PII, and unrestricted vendor payloads out of event details and logs. Rejected/unauthorized API requests are recorded in API security logs; an accepted business command is recorded through the workflow.


## 16. Operator visibility and recovery


### Workflow Query


Implement a read-only query named get_processing_state. It returns bounded state:
- application_id, workflow_id, current run ID.
- business status, current_stage, stage_started_at, waiting_reason.
- Total, pending, successful, document-error, and technical-error document counts.
- IDs of failed documents.
- Current evaluation ID and pinned policy ID/version/hash.
- Active review task, queue, reminder/escalation flags, absolute deadline.
- Last technical error code and failed stage.
- Activation status/account reference where known.
- Available operator actions and recovery deadline.


Queries perform no network/database I/O and cause no state changes. A query depends on an available worker; during worker downtime use Temporal execution visibility and the last persisted Postgres projection. Always label projection timestamps and do not claim stale data is live.


### Search Attributes


Register typed custom Search Attributes before running the demo:
- KycApplicationId: Keyword.
- KycBusinessStatus: Keyword.
- KycStage: Keyword.
- KycWaitingReason: Keyword.
- KycPolicyVersionInt: Int.
- KycReviewDeadlineDate: Datetime.


Set policy/deadline attributes only when applicable; unset stale waiting/deadline values when leaving those states. Do not index names, document contents, or other sensitive applicant data. Search is for operational discovery, not authorization or correctness; visibility indexing is asynchronous.


### Dashboards and alerts


| View or alert | Data/source |
|---|---|
| Open applications by stage and age | Postgres applications projection |
| Review queue, overdue reminders and escalations | review_tasks and audit_events |
| Pinned policy distribution and rule outcomes | evaluations; Search Attributes for open-workflow discovery |
| Pending/retrying Activities and task queue backlog | Temporal UI and service/worker metrics |
| Technical failure waiting for operator | Workflow Query, Temporal visibility, Postgres status |
| Approved but not active | decisions joined to account_activations |
| Missing/stale business projection | Last recorded event/update time compared with execution state |


Show failure codes and stage age, with correlation links/identifiers for the workflow and business audit timeline. Alert on worker unavailability, queue backlog, prolonged retries, review escalation/expiry, persistent Postgres failure, and activation errors.


### Authorized operator commands


All commands go through an authenticated API into validated Temporal Updates. Operators do not edit Postgres status to steer execution.


| Update | Allowed state | Effect |
|---|---|---|
| submit_review_decision | Current review OPEN/ASSIGNED/ESCALATED, before expiry | Accept approve/reject for the exact task and evaluation |
| request_policy_reevaluation | Awaiting review, before expiry | Evaluate an explicitly targeted immutable policy under a new evaluation ID |
| retry_failed_stage | PROCESSING_ERROR or ACTIVATION_ERROR recovery wait | Retry the identified failed operation after operator correction |


Each Update includes a unique request ID, authenticated actor and role, application ID, expected evaluation/task or failed-stage generation, and a reason. Exact duplicate request IDs return the original outcome; reusing a request ID with a different body is rejected. Version/state mismatches are rejected.


After a processing failure, persist partial results and a PROCESSING_ERROR or ACTIVATION_ERROR state, then wait durably for operator recovery until its configured deadline. The workflow remains open during this wait. This explicitly supersedes the v1 behavior of immediately ending on exhausted retries.


An accepted retry increments recovery_generation and uses fresh Activity scheduling IDs while preserving completed results and external idempotency keys. Retry failed documents only; do not repeat successful parses. Retry evaluation with its pinned policy; a different policy requires explicit re-evaluation. A new scheduling generation may restart a stub's fail-first-two Activity-attempt pattern; this must be visible in the audit rather than described as continuation of the previous retry count.


If processing failed after the review deadline was established, recovery cannot extend that deadline. Do not accept recovery after the applicable expiry. If audit/status persistence itself is unavailable, show the error in Temporal; no operator Update may bypass that dependency.


Reset, termination, and namespace deletion are not normal business recovery actions in this demo. Any emergency use needs explicit operator procedure and separate audit because it can bypass normal cleanup and create overlapping executions.


## 17. Policy-change handling across many in-flight workflows


Policy changes use immutable JSON versions, not edits to the workflow code. Current-policy publication affects future successful policy resolutions. A completed app_load_policy result and its snapshot remain fixed on replay and evaluation retry.


Support a bounded re-evaluation campaign for applications awaiting review:
1. Publish and verify v2, then update current.json.
2. An operator/campaign API discovers awaiting-review workflows. Search results may be stale.
3. Send request_policy_reevaluation with campaign/request IDs, actor, reason, expected current evaluation, and explicit target policy ID/version/hash.
4. Each workflow validates its own live state. Record accepted/rejected/deduplicated outcomes per application; do not claim an atomic fleet-wide cutover.
5. On acceptance, block the old review from deciding, allocate evaluation_sequence + 1 and a new evaluation ID, and resolve the explicitly requested immutable policy.
6. Re-run app_evaluate_application using cached extracted facts. Do not re-download documents or repeat completed OCR.
7. In app_persist_assessment, append the new evaluation/findings and audit events, mark the old review SUPERSEDED, and create a new review if required. Retain old evaluations and findings as historical evidence.
8. Resume disposition under the new assessment. Existing OFAC/PEP concerns remain binding review inputs; a credit-age rule passing cannot silently clear them.


app_load_policy accepts either current mode or an explicit version/hash mode. In explicit mode, do not follow a later current pointer. Verify the requested hash matches the immutable object.


Carry forward original review opened/deadline timestamps. Re-evaluation must not reset the reminder/escalation/expiry budget. If review expires during evaluation, persist the evaluation if available but do not accept review or activate the account.


Serialize review-decision and policy-change Updates. If the review decision is accepted first and the application leaves review, reject subsequent in-place re-evaluation. If re-evaluation is accepted first, reject decisions naming the superseded evaluation/task. Never reinterpret an accepted reviewer decision against an unseen policy.


Do not re-evaluate finalized KYC decisions or accounts already activating/active in place. A mandatory policy affecting those customers needs a separately designed remediation process. This limitation and the default grandfathering boundary must be explicit in the interview.


Distinguish numeric/JSON rule changes (supported without workflow-code deployment) from new operators, new data requirements, or altered workflow control flow, which can require new code, schema changes, and replay-compatible deployment.


## 18. Account activation and end-to-end completion


The assignment ends at account activation, not KYC approval.


After app_persist_final_outcome records APPROVED:
1. Its transaction creates a PENDING account_activations row with idempotency key kyc-activate:{application_id}, requested_at, and ACTIVATION_REQUESTED audit event.
2. app_record_event moves the processing projection to ACTIVATING before the external call.
3. app_activate_account invokes the account service stub using the stable application-level idempotency key and approved decision/evaluation reference.
4. app_persist_activation records the provider receipt/account ID, sets activation ACTIVE, sets application ACTIVE and completed_at, and appends ACTIVATION_CONFIRMED in one transaction.
5. Only then does the workflow return successful onboarding.


app_activate_account calls the account service only; it does not write Postgres. app_persist_activation is the separate receipt-persistence operation. The account stub must persist its idempotency-key-to-account mapping and reject a reused key with different activation input.


A lost provider response is retried or reconciled using the same key. A crash after external activation but before database persistence must not create another account. The same key survives Activity retries, operator recovery generations, and replay. Do not generate it from attempt number or run ID.


Schedule-To-Close bounds an activation call's retry envelope; its HTTP timeout stays below Start-To-Close. Exhaustion or permanent service rejection becomes ACTIVATION_ERROR, with approved KYC retained. app_persist_activation accepts a technical/blocked result and appends ACTIVATION_ERROR. Enter the operator recovery wait; do not turn a technical activation failure into KYC rejection.


If receipt persistence fails after provider success, retry that persistence Activity with the recorded receipt, not account creation. If a late provider response is ambiguous, show reconciliation required; do not promise cross-system atomicity.


Rejected, REVIEW_EXPIRED, and unresolved processing-error applications never call activation. No compensation/deactivation is required for the normal approval-then-activation path. A future downstream step that can require reversing activation would need an explicit idempotent compensation design.


app_persist_final_outcome records the KYC decision and completes human review. Its application completion timestamp is set there only for REJECTED; approved applications complete only at ACTIVE (or a separately recorded terminal operational failure). Keep decision time separate from activation time.


The approved-decision prerequisite and matching evaluation/task references are enforced in persistence transactions. SQL foreign keys alone do not enforce that the referenced decision is APPROVED or that a review task belongs to the selected evaluation.


## 19. Assignment coverage and demonstration script


| Assignment theme | Concrete behavior | Demonstration evidence |
|---|---|---|
| Hours/days of onboarding | Durable workflow, external-call retries, long review/recovery waits | Restart workers during parsing/review; resume without recreating applications |
| Timers | Review reminder, escalation, expiry; bounded activation retry envelope | Accelerated 15/30/60-second review thresholds; restart across thresholds |
| HITL | Validated Update, current evaluation/task, deduplication, authenticated reviewer | Approve/reject; duplicate and stale decisions; decision-versus-expiry race |
| Audit trail | Atomic business events, immutable assessments/policy snapshots, actor/reason/correlation | One timeline shows submission identity through approval and activation |
| Operator visibility | Live Query, Search Attributes, business dashboard, worker/service metrics | Answer what is waiting, why, since when, policy version, and available recovery |
| Policy change | Latest-at-resolution plus explicit awaiting-review re-evaluation | Same 60-month fact passes v1 48-month threshold and requires review under v2 72-month threshold |
| Account service | Approval gate, provider idempotency, receipt persistence | Drop the first activation response; verify one account and final ACTIVE |


Required test scenarios:
1. Clean application: parse, evaluate, screen clear, approve, activate exactly one logical account through idempotency.
2. OCR 503 twice then success: observe retries without repeating successful sibling documents.
3. PEP or potential OFAC match: review opens; approve/reject through Update.
4. Unanswered review: reminder, escalation, expiry; no decision or activation fabricated.
5. Worker outage across review timers: overdue state reconciles on restart without deadline reset.
6. Lost activation response: same key retrieves/reuses one account.
7. Activation technical failure: KYC remains approved; operator sees and retries activation.
8. Mid-parse policy publication: next policy resolution uses v2.
9. Awaiting-review campaign: preserve v1, append v2, supersede the old review, reject stale reviewer decisions.
10. Postgres outage at an audit boundary: workflow blocks visibly and does not silently activate.
11. SQL schema deployment and idempotency/transaction integration tests pass.
12. Time-skipping workflow tests verify timers and message races; replay tests verify compatibility of recorded histories.


## 20. Implementation references


- Temporal Python timers, wait conditions, Updates, and time-skipping tests: https://github.com/temporalio/sdk-python/blob/main/README.md
- Temporal Python workflow message passing: https://docs.temporal.io/develop/python/message-passing
- Temporal Visibility and asynchronous indexing: https://docs.temporal.io/visibility
- Temporal execution history and replay: https://docs.temporal.io/encyclopedia/event-history


This is a specification update, not an implemented or executed demo. Database migrations, service stubs, operator APIs, dashboard views, and integration tests remain implementation work.