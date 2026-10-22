"""Payload models shared by the workflow and activities.

Only references and compact results cross the Temporal boundary: no document
bytes, no base64, no raw provider payloads (spec section 6).

Timestamps are ISO-8601 strings produced from workflow time (or by an
Activity, whose recorded result is replay-stable).
"""
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "1.0"

# ---- workflow input / output ----------------------------------------------


@dataclass
class SubmissionRef:
    bucket: str
    key: str
    sha256: str


@dataclass
class KycWorkflowInput:
    application_id: str
    submission_id: str
    submission_ref: SubmissionRef
    schema_version: str = SCHEMA_VERSION
    timer_profile: str = "normal-v1"
    policy_id: str = "onboarding"


@dataclass
class KycResult:
    application_id: str
    # ACTIVE | REJECTED | REVIEW_EXPIRED | PROCESSING_ERROR | ACTIVATION_ERROR
    status: str
    decision: str | None = None
    account_id: str | None = None
    detail: str | None = None


# ---- audit events -----------------------------------------------------------


@dataclass
class AuditEvent:
    event_key: str
    event_sequence: int
    event_type: str
    occurred_at: str
    actor_type: str = "WORKFLOW"  # WORKFLOW | REVIEWER | OPERATOR
    actor_id: str = "kyc-workflow"
    reason: str | None = None
    request_id: str | None = None
    activity_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


# ---- submission / documents ------------------------------------------------


@dataclass
class DocumentRef:
    application_id: str
    document_id: str
    document_type: str
    bucket: str
    object_key: str
    size_bytes: int
    sha256: str
    client_document_reference: str | None = None
    original_filename: str | None = None


@dataclass
class LoadedSubmission:
    application_id: str
    submission_id: str
    submitted_at: str
    declared_applicant: dict[str, Any]
    documents: list[DocumentRef]
    client_reference: str | None = None


@dataclass
class LoadSubmissionInput:
    application_id: str
    submission_id: str
    submission_ref: SubmissionRef


@dataclass
class ParseDocumentInput:
    ref: DocumentRef
    # Stable across Activity retries; changes with a new recovery generation.
    invocation_id: str


@dataclass
class DocumentOutcome:
    document_id: str
    # SUCCEEDED | DOCUMENT_ERROR | TECHNICAL_ERROR | NOT_RUN | PENDING
    status: str
    extracted_fields: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    attempts: int = 0
    extracted_at: str | None = None


# ---- policy / evaluation ----------------------------------------------------


@dataclass
class LoadPolicyInput:
    policy_id: str
    mode: str = "current"  # current | explicit
    version: int | None = None
    sha256: str | None = None


@dataclass
class PolicySnapshot:
    policy_id: str
    version: int
    bucket: str
    object_key: str
    sha256: str
    policy: dict[str, Any]
    resolved_at: str


@dataclass
class EvaluationDocument:
    document_id: str
    document_type: str
    status: str
    extracted_fields: dict[str, Any] | None = None


@dataclass
class EvaluateInput:
    evaluation_id: str
    evaluation_sequence: int
    declared_applicant: dict[str, Any]
    documents: list[EvaluationDocument]
    policy: PolicySnapshot
    evaluated_at: str


@dataclass
class Finding:
    finding_key: str
    source: str  # DOCUMENT | POLICY | SCREENING
    finding_type: str
    severity: str  # INFO | WARNING | BLOCKING
    description: str
    document_id: str | None = None
    rule_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvaluationResult:
    evaluation_id: str
    evaluation_sequence: int
    evaluated_facts: dict[str, Any]
    rule_results: list[dict[str, Any]]
    findings: list[Finding]
    evaluation_outcome: str  # PASS | REVIEW
    review_required: bool
    evaluated_at: str
    policy: PolicySnapshot | None = None


# ---- screening -------------------------------------------------------------


@dataclass
class ScreenInput:
    application_id: str
    client_reference: str | None
    declared_applicant: dict[str, Any]
    invocation_id: str


@dataclass
class ScreeningResult:
    # SUCCEEDED | TECHNICAL_ERROR | NOT_RUN | PENDING
    status: str
    potential_ofac_match: bool | None = None
    pep_flag: bool | None = None
    result_details: dict[str, Any] | None = None
    provider_reference: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    attempts: int = 0
    screened_at: str | None = None


# ---- persistence inputs -----------------------------------------------------


@dataclass
class StartProcessingInput:
    application_id: str
    submission_id: str
    submission_ref: SubmissionRef
    workflow_id: str
    run_id: str
    initial_run_id: str
    processing_started_at: str
    event: AuditEvent


@dataclass
class Projection:
    """Column updates for kyc.applications; None means SQL NULL."""

    set_fields: dict[str, str | None] = field(default_factory=dict)


@dataclass
class ReviewMilestone:
    review_task_id: str
    kind: str  # REMINDER | ESCALATION | EXPIRY
    at: str
    assigned_queue: str | None = None
    historical: bool = False


@dataclass
class RecordEventInput:
    application_id: str
    workflow_id: str
    run_id: str
    events: list[AuditEvent]
    projection: Projection = field(default_factory=Projection)
    milestone: ReviewMilestone | None = None


@dataclass
class ReviewTaskSpec:
    review_task_id: str
    evaluation_id: str
    review_key: str
    reason: str
    opened_at: str
    reminder_due_at: str
    escalation_due_at: str
    expires_at: str
    status: str = "OPEN"
    reminded_at: str | None = None
    escalated_at: str | None = None
    assigned_to: str | None = None


@dataclass
class PersistAssessmentInput:
    application_id: str
    workflow_id: str
    run_id: str
    target_status: str  # PROCESSING | AWAITING_REVIEW | PROCESSING_ERROR
    current_stage: str
    events: list[AuditEvent]
    waiting_reason: str | None = None
    loaded: LoadedSubmission | None = None
    documents: list[DocumentRef] = field(default_factory=list)
    document_outcomes: list[DocumentOutcome] = field(default_factory=list)
    screening: ScreeningResult | None = None
    evaluation: EvaluationResult | None = None
    review: ReviewTaskSpec | None = None
    supersede_review_task_id: str | None = None
    review_deadline_at: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class AssessmentPersisted:
    application_id: str
    status: str
    evaluation_id: str | None = None
    review_task_id: str | None = None


@dataclass
class FinalOutcomeInput:
    application_id: str
    workflow_id: str
    run_id: str
    evaluation_id: str
    decision: str  # APPROVED | REJECTED
    decision_source: str  # AUTOMATIC | HUMAN
    reason_code: str
    decided_by: str
    decided_at: str
    events: list[AuditEvent]
    review_task_id: str | None = None
    reason_details: str | None = None
    review_request_id: str | None = None
    resolution_notes: str | None = None
    activation_idempotency_key: str | None = None


@dataclass
class FinalOutcomePersisted:
    application_id: str
    decision: str
    application_status: str


@dataclass
class ActivateAccountInput:
    application_id: str
    idempotency_key: str
    evaluation_id: str
    generation: int = 0


@dataclass
class ActivationReceipt:
    account_id: str
    provider_reference: str | None
    activated_at: str
    confirmed_at: str


@dataclass
class PersistActivationInput:
    application_id: str
    workflow_id: str
    run_id: str
    result: str  # ACTIVE | TECHNICAL_ERROR | BLOCKED
    events: list[AuditEvent]
    receipt: ActivationReceipt | None = None
    error_code: str | None = None
    error_message: str | None = None


# ---- Updates (operator / reviewer commands) --------------------------------


@dataclass
class ReviewDecisionRequest:
    request_id: str
    application_id: str
    review_task_id: str
    evaluation_id: str
    decision: str  # APPROVE | REJECT
    actor_id: str
    actor_role: str
    reason: str
    notes: str | None = None


@dataclass
class ReevaluationRequest:
    request_id: str
    application_id: str
    expected_evaluation_id: str
    policy_id: str
    policy_version: int
    policy_sha256: str
    actor_id: str
    actor_role: str
    reason: str
    campaign_id: str | None = None


@dataclass
class RetryRequest:
    request_id: str
    application_id: str
    failed_stage: str
    recovery_generation: int
    actor_id: str
    actor_role: str
    reason: str


@dataclass
class UpdateOutcome:
    request_id: str
    status: str  # ACCEPTED
    state: str  # e.g. DECISION_RECORDED | REEVALUATED | RETRY_SCHEDULED
    detail: dict[str, Any] = field(default_factory=dict)


# ---- Query ------------------------------------------------------------------


@dataclass
class ProcessingState:
    application_id: str
    workflow_id: str
    run_id: str
    business_status: str
    current_stage: str
    stage_started_at: str
    waiting_reason: str | None = None
    documents_total: int = 0
    documents_pending: int = 0
    documents_succeeded: int = 0
    documents_document_error: int = 0
    documents_technical_error: int = 0
    failed_document_ids: list[str] = field(default_factory=list)
    evaluation_id: str | None = None
    policy_id: str | None = None
    policy_version: int | None = None
    policy_sha256: str | None = None
    review_task_id: str | None = None
    review_queue: str | None = None
    review_reminded: bool = False
    review_escalated: bool = False
    review_deadline_at: str | None = None
    last_error_code: str | None = None
    failed_stage: str | None = None
    activation_status: str | None = None
    account_id: str | None = None
    recovery_generation: int = 0
    recovery_deadline_at: str | None = None
    available_actions: list[str] = field(default_factory=list)
    timer_profile: str = "normal-v1"
