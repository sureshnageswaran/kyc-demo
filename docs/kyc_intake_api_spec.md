# Temporal KYC Demo — Intake API Specification

Version: 1.2 draft · 20 October 2026

## 1. Goal and scope

Build a fresh Python FastAPI intake service for a synthetic individual-customer KYC demo. The service stores applicant declarations, uploaded document bytes, and application metadata in one existing local Garage S3 bucket. An explicit submission starts asynchronous processing through Temporal.

The seeder will behave like a web client: create an application, upload any number of documents within configured limits, then press the equivalent of Submit. The seeder must never write directly to Garage or Temporal.

### Overall Project Background
This project is a demo of how KYC can work at a Fintech/Bank using Temporal. The key is to showcase what features Temporal can bring to bear, and all KYC related services like the seeder (for data seeding), the Intake API, the OCR service, the OFAC/PEP evaluation service are all mock-up stubs and are built to return values as desired, so as to demonstrate particular scenarios.

### About this Document
This document specifies intake and the handoff contract only. Downstream processing, KYC decisions, human review, the UI, and the seeder implementation are separate work. The initial business scope is individual applicants; corporate ownership, beneficial owners, and entity KYC are excluded.

### Core decisions

| Question | Decision |
| --- | --- |
| When is the application ID assigned? | On successful application creation. |
| Does creation know how many documents will arrive? | No. Initially the document count is zero. |
| Where are application and document records stored? | One configured Garage bucket, with keys grouped by application ID. |
| Is a bucket created per application? | No. An application ID is a logical prefix, not a bucket ID. |
| When is the document set complete? | When the client calls the explicit submit endpoint. |
| Does uploading a document start processing? | No. Upload acknowledges persistence only. |
| Are uploads asynchronous document jobs? | No. Use a document resource, not a misleading job name. |
| When does Temporal start? | After a durable submission snapshot exists. |
| Does intake decide whether KYC passes? | No. It validates the request envelope and storage readiness. |
| Does intake read or write Postgres? | No. Garage is the sole durable store for intake. |

### Persistence and ownership boundary

Intake has no Postgres client, connection settings, tables, migrations, reads, or writes. Its only durable persistence dependency is Garage. In-memory locks and lookup caches are rebuilt from Garage after restart.

| Stage | Owner | Persistence |
| --- | --- | --- |
| Application creation and document upload | Intake API | Garage application JSON, document bytes, document metadata |
| Submission and workflow handoff receipt | Intake API | Garage submission snapshot and dispatch receipt |
| Application processing and business status/results | Temporal worker Activities | Postgres, when the processing design requires it |
| Workflow execution history | Temporal service | Temporal's separately configured persistence |

If a processing application record is needed in Postgres, a worker Activity creates it idempotently using application_id after Temporal starts. Intake does not create a placeholder row first. Activities own business database effects; the Temporal service's internal persistence is a separate infrastructure concern.

The intake responsibility ends at confirmed workflow start. A minimal Garage dispatch receipt remains necessary to reconcile submission/start failures; it is handoff bookkeeping, not business processing state. No processing service is required to exercise create, upload, close, or the handoff contract using a test double for the Temporal client.

## 2. Endpoints and lifecycle

| Method | Path | Purpose | Normal status |
| --- | --- | --- | --- |
| GET | `/health` | Process liveness | 200 |
| GET | `/ready` | Garage availability and dispatcher readiness | 200 / 503 |
| POST | `/v1/applications` | Persist applicant declarations and return application ID | 201 |
| POST | `/v1/applications/{application_id}/documents` | Upload one document and return document ID | 201 |
| GET | `/v1/applications/{application_id}` | Inspect intake state, documents, and workflow handoff | 200 |
| POST | `/v1/applications/{application_id}/submit` | Freeze the document set and request Temporal processing | 202 |

These resource-oriented routes express the proposed create-application, document-jobs, and finish-application operations. No aliases are needed in v1.

Application intake states:

- `DRAFT`: creation succeeded; uploads are allowed.
- `SUBMITTED`: an immutable submission snapshot exists; uploads are forbidden.

Workflow dispatch is a separate state: `NOT_REQUESTED`, `PENDING`, `STARTED`, or `BLOCKED`. `SUBMITTED` is not a KYC approval and `STARTED` is not processing completion. Processing outcomes belong to Temporal and a later results contract.

The submission object is the authoritative boundary: its existence means the application is submitted, even if a cached status elsewhere says DRAFT. Read and mutation paths must check it. There is no reopen, document replacement, or resubmit-as-new-run operation in v1. A new demo run creates a new application ID.

## 3. Common conventions

- JSON fields use snake_case; stored JSON includes `schema_version: "1.0"`.
- IDs are server-generated UUIDv4 strings. Examples below use complete UUID-shaped values.
- Times are server-generated UTC RFC 3339 strings; dates of birth use `YYYY-MM-DD`.
- Unknown fields are rejected. Optional fields may be omitted; explicit null is accepted only where specified.
- Every POST requires an `Idempotency-Key` header, 1–128 printable ASCII characters. Missing or malformed keys return 422.
- Each request has a server request ID returned in `X-Request-ID` and error bodies.
- All sample people, contact information, documents, and identifiers are synthetic.
- Caller-supplied bucket names, object keys, application IDs on create, or document IDs on upload are rejected. The service constructs storage paths.

## 4. Create application

`POST /v1/applications` · `Content-Type: application/json`

Example request:

```json
{
  "client_reference": "seed-run-001-app-0001",
  "applicant": {
    "given_name": "Avery",
    "middle_name": "Morgan",
    "family_name": "Example",
    "date_of_birth": "1988-04-16",
    "residential_address": {
      "line1": "100 Example Lane",
      "line2": "Unit 2",
      "city": "Metuchen",
      "region": "NJ",
      "postal_code": "08840",
      "country_code": "US"
    },
    "email": "avery.example@example.com",
    "phone": "+12025550124"
  }
}
```

Required fields: `applicant`, given_name, family_name, date_of_birth, and residential_address with line1, city, region, postal_code, country_code. middle_name, line2, email, phone, and client_reference are optional. In v1, country_code is `US`; broader address formats are future work.

Constraints: names 1–100 characters after trimming; address lines 1–200; city/region 1–100; postal_code 1–20; client_reference 1–128. Date of birth must be a valid past date. Email, if supplied, must be syntactically valid; phone, if supplied, is E.164 formatted. Do not implement an adult-age rule or KYC eligibility decision at intake. Do not collect a real SSN or full tax ID for this demo.

The applicant object is the customer's **declared information**. Preserve it unchanged in the application record and submission snapshot.

On success, persist one application record before responding. Return `Location: /v1/applications/{application_id}` and:

```json
{
  "application_id": "11111111-1111-4111-8111-111111111111",
  "client_reference": "seed-run-001-app-0001",
  "intake_status": "DRAFT",
  "document_count": 0,
  "created_at": "2026-10-20T10:15:00Z",
  "links": {
    "self": "/v1/applications/11111111-1111-4111-8111-111111111111",
    "documents": "/v1/applications/11111111-1111-4111-8111-111111111111/documents",
    "submit": "/v1/applications/11111111-1111-4111-8111-111111111111/submit"
  }
}
```

No expected document count or promise of three documents is returned. client_reference is a correlation label, not a uniqueness or idempotency constraint.

## 5. Upload a document

`POST /v1/applications/{application_id}/documents` · `multipart/form-data`

One file per request. Repeating the endpoint adds documents to the same application. A client may upload two, 22, or any other count within configuration.

| Form field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `file` | Uploaded bytes | Yes | Non-empty synthetic document, filename ending .pdf and media type application/pdf |
| `document_type` | String enum | Yes | `passport`, `drivers_license`, `national_id`, `utility_bill`, `bank_statement`, `other` |
| `client_document_reference` | String, 1–128 | No | Seeder/UI correlation label |

Persist client_document_reference unchanged in document.json, include it in application inspection responses, and copy it into the submission snapshot. Persist client_reference unchanged in application.json and the submission snapshot. Both reference fields are opaque strings: intake does not parse or act on their contents.

A file upload with form metadata is multipart, not a separate JSON request body. Uploaded document_type is a caller declaration; it is not verified classification.

Default limits: 10 MiB per file, 100 documents per application, 100 MiB total document bytes per application. All three are configurable. Stream and count bytes; never trust Content-Length alone. Serialize the final limit check and document commit with other mutations on the application.

Intake checks metadata, size, and non-empty content. This demo service does **not** parse PDF structure or require a PDF signature: placeholder bytes deliberately labelled as PDF are accepted. File contents are opaque to intake; it stores and checksums the bytes without interpreting them.

Store the original filename as display metadata only, at most 255 characters; never use it as an object key or filesystem path. Compute SHA-256 from the actual bytes. The same bytes uploaded with a different Idempotency-Key create a different document; checksum deduplication is not automatic.

Example response:

```json
{
  "application_id": "11111111-1111-4111-8111-111111111111",
  "document_id": "22222222-2222-4222-8222-222222222222",
  "document_type": "passport",
  "original_filename": "synthetic-passport.pdf",
  "content_type": "application/pdf",
  "size_bytes": 64,
  "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "upload_status": "STORED",
  "uploaded_at": "2026-10-20T10:16:00Z"
}
```

The example digest and size are illustrative; implementation computes them. Upload success requires both blob and document metadata persistence. It does not start processing or a Temporal workflow.

## 6. Submit application

`POST /v1/applications/{application_id}/submit` · JSON body `{}`

Submit means: “The client has finished uploading this application's document set.” It is the only trigger for Temporal. It does not mean that every required KYC document is present or correct.

Under the application mutation lock:

1. Confirm the application exists. If already submitted, return its existing submission and handoff status without creating another workflow.
2. Discover committed document records. Require at least one document, enforce limits, and verify referenced blobs exist with expected lengths. An inconsistent stored package is a storage error, not a KYC failure.
3. Construct the submission snapshot containing the original applicant declarations, ordered document references and metadata, schema version, submission time, submission ID, and deterministic workflow ID.
4. Persist the complete snapshot at the fixed submission key. This single successful object write is the submission commit. From this point forward, reject uploads.
5. Attempt to start Temporal immediately with a bounded RPC timeout. A recovery dispatcher also retries durable submissions that lack confirmed start receipts.
6. Return 202 once the snapshot is durably committed, with dispatch_status STARTED, PENDING, or BLOCKED as currently known. Do not wait for workflow completion.

Response example:

```json
{
  "application_id": "11111111-1111-4111-8111-111111111111",
  "submission_id": "33333333-3333-4333-8333-333333333333",
  "intake_status": "SUBMITTED",
  "document_count": 2,
  "submitted_at": "2026-10-20T10:17:00Z",
  "workflow": {
    "workflow_id": "kyc-11111111-1111-4111-8111-111111111111",
    "dispatch_status": "STARTED",
    "run_id": "44444444-4444-4444-8444-444444444444"
  }
}
```

For PENDING or BLOCKED, run_id is null. Include a sanitized `dispatch_error_code` when known. A Temporal outage after the submission commit yields 202/PENDING, not an instruction to recreate the application. Failure to persist the snapshot yields 503; the caller retries with the same key. If the write outcome is uncertain, reread the fixed submission key before attempting a new commit.

No specific document types are mandatory at this boundary. For example, a submitted passport without proof of address is a legitimate downstream missing-document scenario. Business document requirements belong in the KYC workflow.

## 7. Inspect an application

`GET /v1/applications/{application_id}`

Return application_id, client_reference, original applicant declarations, intake_status, created_at, submitted_at (nullable), document_count, documents array, and workflow {workflow_id, dispatch_status, run_id, dispatch_error_code}. Each document includes the upload response metadata. Before submission, workflow_id and run_id are null and dispatch_status is NOT_REQUESTED.

This endpoint is not a completed KYC outcome API. It reports handoff status, not live workflow execution status. Derive document_count from committed records for drafts and from the frozen snapshot after submission. GET returns 404 for an unknown application.

## 8. Garage object layout and commit rules

One pre-created configurable bucket, example `kyc-demo`. Garage endpoint, credentials, region, and bucket are configured at startup. Fail readiness if the bucket is missing; do not silently create it.

| Object key | Contents / authority |
| --- | --- |
| `applications/{application_id}/application.json` | Immutable creation record, declared applicant data, creation request fingerprint |
| `applications/{application_id}/documents/{document_id}/content.pdf` | Original uploaded bytes |
| `applications/{application_id}/documents/{document_id}/document.json` | Immutable committed document record, blob reference, checksum, upload request fingerprint |
| `applications/{application_id}/submission.json` | Immutable submission snapshot and durable dispatch intent |
| `applications/{application_id}/dispatch.json` | Mutable dispatch receipt, attempts, last error, next retry time, confirmed run ID |

Document metadata and submission snapshots store bucket and object key references, not expiring presigned URLs. The submission snapshot embeds each document's intake metadata, so the workflow receives a fixed input package. Include SHA-256 for each blob and for the serialized submission bytes. The submission hash is supplied in the workflow input; it is not embedded into the bytes it hashes.

Upload write order is blob first, then document.json. The metadata write is the document commit. An orphan blob without document.json is ignored by listing and submit. Do not acknowledge it as uploaded. A retry can safely reuse its reserved document ID or overwrite uncommitted bytes under that ID before committing. Once committed, blobs and metadata must never be overwritten by intake.

There is no mutable manifest.json rewritten after every upload: that would introduce lost updates and another source of truth. Draft document listing uses paginated object listing to find committed document records, scoped to the application. Submission freezes an explicit list, so the workflow never discovers its work by listing a live prefix.

### Deployment boundary

This bucket-only design intentionally supports **one intake process, one Uvicorn worker, and one active instance**. Use a process-level exclusive lock on a configured local lock file to reject accidental duplicate startup on the same host, per-application async locks for all mutations, and per-idempotency-key locks for creates. The dispatcher shares those locks. Concurrent requests for different applications remain possible.

No other process writes intake-owned objects. Disable reload for acceptance runs. This is not a distributed locking mechanism; two hosts pointing to the same bucket violate the design. Scaling to multiple intake workers requires transactional coordination (for example Postgres), rather than assuming S3 is a transactional database.

Before implementation is accepted, verify the deployed Garage version's supported S3 operations and visibility behavior. This design requires acknowledged PUTs to be visible to subsequent GET/HEAD and listings in the single local deployment. It does not assume AWS-specific conditional writes, object versioning, bucket events, or multi-object transactions. If listing visibility cannot meet the commit/recovery checks, add an operational index before claiming reliability.

## 9. Idempotency and uncertain responses

Idempotency is persistent, not an in-memory response cache.

| Operation | Key scope | Persistent recovery lookup |
| --- | --- | --- |
| Create | Endpoint + hashed Idempotency-Key | Creation record stores key hash and request fingerprint; startup rebuilds the lookup from creation records |
| Upload | Application + hashed Idempotency-Key | Committed document.json stores key hash and request fingerprint |
| Submit | Application | The one fixed submission.json; key hash retained for audit |

For create and upload, the same key and same logical payload return the existing resource ID with status 200 and `Idempotent-Replayed: true`. Reuse with a different payload returns 409 IDEMPOTENCY_CONFLICT. Fingerprint validated canonical JSON for create; for upload include document_type, client_document_reference, original filename, content type, and SHA-256 of uploaded bytes. Never persist a raw idempotency key in logs.

On submit, all valid repeated requests return 202 and the existing submission regardless of key, because only one submission is permitted per application. Dispatch status may have advanced since the earlier response.

An upload replay that resolves to an already committed document is allowed after submission; a new upload is rejected. Resolve idempotent replays before rejecting based on SUBMITTED state.

Keep key mappings in memory for speed, rebuild them at startup by paginated scans, and confirm authoritative objects when outcomes are uncertain. Do not serve writes until recovery completes. On a storage timeout, reconcile the record before releasing its key reservation for another attempt. If reconciliation cannot establish the outcome, return 503 and keep that key blocked until reconciliation succeeds; never allocate a second resource under the same unresolved key. Raw orphan blobs are not successful documents.

This scan-based approach is acceptable for a local demo, not a high-volume production index. Retain mappings as long as the corresponding records exist. Manual removal of bucket objects or namespace reset invalidates these guarantees.

## 10. Temporal handoff and recovery

Workflow type: `KycApplicationWorkflow`; task queue: `kyc-applications`; workflow ID: `kyc-{application_id}`. Namespace and target are configurable. UUID-based application IDs avoid collisions across seeder runs.

Workflow input:

```json
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
```

The future worker loads the snapshot and document bytes through Activities. Workflow code performs no S3 or other network I/O directly. Downstream processing steps are outside this intake contract. Do not place file bytes or the entire applicant package into the start payload or workflow metadata.

Configure start to reject running-ID conflicts and reject reuse of retained closed workflow IDs (`WorkflowIDConflictPolicy.FAIL` and `WorkflowIDReusePolicy.REJECT_DUPLICATE` in the Python SDK). Handle an existing execution as a possible successful prior start, not as permission to create another workflow ID. Before recording it as the matching execution, verify non-sensitive start metadata containing application_id, submission_id, and submission hash. An identity mismatch becomes BLOCKED and requires operator correction.

Recovery dispatcher:

1. Scan submission objects at startup, then periodically (default every five seconds, with pagination).
2. For submissions without a STARTED receipt, describe the deterministic workflow ID before retrying an uncertain start.
3. If the execution matches, persist STARTED with the initial run ID. If absent and safe to start, retry the same ID and identical input.
4. Persist transient failures as PENDING with bounded exponential backoff and jitter (initial one second, cap 30 seconds). Continue retrying while the service is running.
5. Persist permanent configuration/authentication errors as BLOCKED; expose the code through GET, log it, and recheck after restart or a configuration fix. Do not silently lose the submission.

There is no atomic transaction spanning Garage and Temporal. This is a durable intent plus retry design. A crash after the snapshot write but before start is recovered from the snapshot; a crash after start but before dispatch receipt is recovered from the workflow ID.

Temporal duplicate rejection is bounded by retained execution history. Use a 30-day namespace retention target for the demo and a conservative 7-day maximum automatic dispatch age. An unresolved older submission becomes BLOCKED / DISPATCH_RECONCILIATION_REQUIRED; never automatically start it after the deduplication window may have passed. Keep confirmed STARTED receipts and never redispatch them. Do not claim unconditional exactly-once behavior, especially after manual deletion, namespace resets, or retention expiry.

An unavailable KYC worker is different from an unavailable Temporal server: Temporal can accept the start while work waits for a worker. Intake must not mark a valid start as failed simply because processing has not begun.

## 11. Health, readiness, errors, and configuration

`GET /health` returns 200 `{"status":"ok","service":"kyc-intake"}` if the API process is alive. It does not call dependencies.

`GET /ready` returns component states for garage, recovery scan, dispatcher, and temporal. Return 503 if Garage cannot support intake, startup recovery is incomplete, or the dispatcher loop is not running. If only Temporal is down, return 200 with overall `status: "degraded"`: durable submissions are still accepted and queued. Do not claim write permission is verified by a read-only bucket probe; verify PUT/GET/HEAD/LIST permissions during deployment acceptance.

Standard error body, including FastAPI validation failures:

```json
{
  "error": {
    "code": "APPLICATION_ALREADY_SUBMITTED",
    "message": "New documents cannot be added after submission.",
    "request_id": "55555555-5555-4555-8555-555555555555",
    "retryable": false,
    "details": {}
  }
}
```

| HTTP status | Codes / use |
| --- | --- |
| 404 | APPLICATION_NOT_FOUND |
| 409 | APPLICATION_ALREADY_SUBMITTED, IDEMPOTENCY_CONFLICT, DOCUMENT_LIMIT_EXCEEDED |
| 413 | FILE_TOO_LARGE, APPLICATION_SIZE_LIMIT_EXCEEDED |
| 415 | UNSUPPORTED_MEDIA_TYPE |
| 422 | VALIDATION_ERROR, NO_DOCUMENTS |
| 503 | STORAGE_UNAVAILABLE, PACKAGE_STORAGE_INCONSISTENT, INTAKE_NOT_READY, WRITE_OUTCOME_UNRESOLVED |
| 500 | INTERNAL_ERROR; sanitized details only |

An existing but inconsistent stored package returns a retryable 503 and is surfaced for operator repair. Do not leak credentials, applicant data, file content, or backend exception strings in logs or errors.

Required configuration: S3_ENDPOINT_URL, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_REGION, S3_BUCKET, TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE. Defaults: S3_ADDRESSING_STYLE=path; TEMPORAL_TASK_QUEUE=kyc-applications; MAX_FILE_BYTES=10485760; MAX_DOCUMENTS_PER_APPLICATION=100; MAX_APPLICATION_BYTES=104857600; DISPATCH_SCAN_INTERVAL_SECONDS=5; DISPATCH_RPC_TIMEOUT_SECONDS=5; MAX_AUTOMATIC_DISPATCH_AGE_SECONDS=604800; INTAKE_LOCK_FILE points to a local writable path. Pin implementation dependencies when building, after checking compatibility with the local services.

Bind to loopback by default. Authentication and public exposure are outside this local demo's scope. Restrict access to synthetic data and keep Garage credentials in environment configuration, never in request payloads or generated artifacts.

## 12. Seeder contract for the next phase

The seeder is a Python API client, not another persistence service. For each application it:

1. Generates synthetic declared applicant information and a unique client_reference.
2. Calls create with a stable key derived from seed_run_id + application ordinal.
3. Uploads a chosen number of synthetic documents with stable per-document keys.
4. Calls submit only after all intended uploads have succeeded.
5. Records application_id, document IDs, submission_id, workflow_id, and observed handoff status.

Re-running a partially completed seed run with the same generated payloads and keys must recover IDs and finish the intended operations. Persist or deterministically regenerate payloads; do not generate fresh random data under an old key. A deliberately new run uses a new seed_run_id.

The later seeder spec should configure application count, concurrency, document-count distribution, random seed, and submit versus draft-only mode. Intake defines no dedicated downstream behavior fields and does not interpret processing scenarios. Caller reference strings and uploaded bytes are preserved as opaque data, including any conventions used by demo clients; their interpretation belongs to downstream services.

## 13. Acceptance criteria

1. Create persists declared data in Garage and returns a new application ID, DRAFT, and zero documents.
2. Upload two documents to one application and 22 to another; each document has its own ID and resolves to the correct application prefix.
3. Inspect returns accurate counts. No workflow starts during creation or upload.
4. Submit freezes the committed document list and requests one deterministic workflow ID. Empty drafts cannot submit; missing business document categories can.
5. Identical create/upload retries return the same IDs after process restart; changed payloads under the same key return 409.
6. Repeated and concurrent submit calls produce one submission and one initial workflow execution. A new upload after submission fails; a replay of a committed upload succeeds.
7. Race an upload against submit: a committed document is either included before the boundary, or the upload is rejected after it. No success response may refer to a newly committed document excluded from an already frozen submission.
8. Stop intake after blob write but before metadata write: the orphan is not counted or submitted, and retry can complete safely.
9. Stop intake after snapshot commit but before Temporal start, then restart: recovery starts the same workflow ID.
10. Stop intake after Temporal accepts start but before receipt persistence: restart finds the matching execution without a second start. A timed-out start RPC is handled the same way.
11. Start with Temporal down: create/upload remain available; submit returns 202/PENDING and processing starts after recovery. A stopped worker leaves a confirmed start waiting in Temporal.
12. Disable Garage or force an ambiguous storage response: never acknowledge an unconfirmed record, never create duplicate IDs under the same unresolved key, and expose a retryable error.
13. Placeholder bytes upload successfully without PDF parsing. Stored bytes match the upload checksum, and declared applicant data remains unchanged.
14. Test size/count limits, malformed multipart or application JSON, unknown IDs, and startup refusal for a second local intake process.
15. An unresolved submission older than the configured dispatch age becomes BLOCKED instead of being automatically restarted. Confirmed STARTED receipts are never redispatched.

16. Run all intake contract tests with Garage and a fake Temporal client, with no Postgres or downstream processing services. Verify the start call references the frozen package and is made only after submission commits. A separate integration check uses a minimal Temporal worker that accepts the input and completes without business processing.
17. Verify intake has no Postgres dependency or connection attempt. Processing database records, if required later, are created only by worker Activities.

## 14. Implementation references

These references guide implementation; the API choices and single-process constraints above are deliberate design decisions.

- FastAPI multipart files and form fields: https://fastapi.tiangolo.com/tutorial/request-forms-and-files/
- Temporal Python client start options: https://python.temporal.io/temporalio.client.Client.html
- Temporal Python workflow ID conflict policy: https://python.temporal.io/temporalio.common.WorkflowIDConflictPolicy.html
- Temporal Python workflow ID reuse policy: https://python.temporal.io/temporalio.common.WorkflowIDReusePolicy.html
- Garage S3 compatibility reference: https://github.com/deuxfleurs-org/garage/blob/main-v2/doc/book/reference-manual/s3-compatibility.md

Next deliverable: implement this intake API against the local Garage and Temporal instances, then specify and build the seeder against the API contract.
