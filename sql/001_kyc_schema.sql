-- KYC demo: Postgres schema for the Temporal KYC workflow.
-- Source: docs/kyc_workflow_and_postgres_spec.md section 9 (v1.1).
--
-- Idempotent: safe to re-run. Apply with sql/apply_schema.sh, or directly:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/001_kyc_schema.sql
--
-- Temporal's own persistence is a separate database; do not run this there.

SET client_min_messages = warning;
BEGIN;

CREATE SCHEMA IF NOT EXISTS kyc;

CREATE TABLE IF NOT EXISTS kyc.applications (
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

CREATE TABLE IF NOT EXISTS kyc.documents (
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

CREATE TABLE IF NOT EXISTS kyc.screenings (
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

CREATE TABLE IF NOT EXISTS kyc.evaluations (
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

CREATE TABLE IF NOT EXISTS kyc.findings (
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

CREATE TABLE IF NOT EXISTS kyc.review_tasks (
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

CREATE TABLE IF NOT EXISTS kyc.decisions (
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

CREATE INDEX IF NOT EXISTS applications_status_started_idx
    ON kyc.applications (status, processing_started_at);

CREATE INDEX IF NOT EXISTS applications_client_reference_idx
    ON kyc.applications (client_reference);

CREATE INDEX IF NOT EXISTS review_tasks_queue_idx
    ON kyc.review_tasks (status, created_at);

CREATE INDEX IF NOT EXISTS findings_open_idx
    ON kyc.findings (application_id, evaluation_id)
    WHERE disposition = 'OPEN';

CREATE INDEX IF NOT EXISTS evaluations_policy_idx
    ON kyc.evaluations (policy_id, policy_version);

CREATE TABLE IF NOT EXISTS kyc.account_activations (
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

CREATE TABLE IF NOT EXISTS kyc.audit_events (
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

CREATE INDEX IF NOT EXISTS review_tasks_expiry_idx
    ON kyc.review_tasks (expires_at)
    WHERE status IN ('OPEN', 'ASSIGNED', 'ESCALATED');

CREATE INDEX IF NOT EXISTS applications_stage_age_idx
    ON kyc.applications (current_stage, stage_started_at);

CREATE INDEX IF NOT EXISTS audit_events_timeline_idx
    ON kyc.audit_events (application_id, occurred_at);

CREATE INDEX IF NOT EXISTS account_activations_status_idx
    ON kyc.account_activations (status, requested_at);


-- ---------------------------------------------------------------------------
-- audit_events is application-append-only (not a WORM store): reject
-- UPDATE/DELETE/TRUNCATE from any role, including the runtime role.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION kyc.audit_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'kyc.audit_events is append-only (% rejected)', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;

DROP TRIGGER IF EXISTS audit_events_no_mutation ON kyc.audit_events;
CREATE TRIGGER audit_events_no_mutation
    BEFORE UPDATE OR DELETE ON kyc.audit_events
    FOR EACH ROW EXECUTE FUNCTION kyc.audit_events_append_only();

DROP TRIGGER IF EXISTS audit_events_no_truncate ON kyc.audit_events;
CREATE TRIGGER audit_events_no_truncate
    BEFORE TRUNCATE ON kyc.audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION kyc.audit_events_append_only();

-- ---------------------------------------------------------------------------
-- Dashboard views (spec section 12 / 16).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW kyc.v_open_applications AS
SELECT application_id, client_reference, status, current_stage, waiting_reason,
       stage_started_at, now() - stage_started_at AS stage_age,
       review_deadline_at, last_error_code
FROM kyc.applications
WHERE status IN ('PROCESSING', 'AWAITING_REVIEW', 'ACTIVATING',
                 'PROCESSING_ERROR', 'ACTIVATION_ERROR');

CREATE OR REPLACE VIEW kyc.v_review_queue AS
SELECT t.application_id, t.review_task_id, t.status, t.assigned_to,
       t.created_at, t.reminder_due_at, t.reminded_at,
       t.escalation_due_at, t.escalated_at, t.expires_at
FROM kyc.review_tasks t
WHERE t.status IN ('OPEN', 'ASSIGNED', 'ESCALATED');

CREATE OR REPLACE VIEW kyc.v_assessments_by_policy AS
SELECT policy_id, policy_version, evaluation_outcome, count(*) AS evaluations
FROM kyc.evaluations
GROUP BY policy_id, policy_version, evaluation_outcome;

CREATE OR REPLACE VIEW kyc.v_approved_not_active AS
SELECT d.application_id, d.decided_at, a.status AS activation_status,
       a.requested_at, a.error_code
FROM kyc.decisions d
LEFT JOIN kyc.account_activations a USING (application_id)
WHERE d.decision = 'APPROVED'
  AND (a.status IS NULL OR a.status <> 'ACTIVE');

COMMIT;
