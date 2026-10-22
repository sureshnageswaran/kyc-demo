# KYC Data Seeder — Python Script Specification

Version: 1.0 draft · 20 October 2026

## 1. Purpose and boundaries

Implement `kyc-data-seeder.py`, a command-line Python client that creates synthetic KYC applications through the intake API at a configured rate. For each application it uploads synthetic files and submits the completed package, causing intake to request a Temporal workflow.

The seeder calls only intake HTTP endpoints. It has no Garage credentials, Temporal client, Postgres client, or direct access to downstream stub administration. It must be usable before the processing services exist.

The seeder marks applications and documents so future OCR and OFAC/PEP screening stubs can produce reproducible outcomes. Intake preserves these markings as opaque existing reference strings; it neither understands scenarios nor implements failures. No new intake processing-control fields are required.

This spec defines the scenario convention those future stubs and their calling Activities must support. It does not claim those integrations already exist, and does not define their complete APIs. The screening Activity name is `screen_applicant`.

## 2. Invocation and command-line options

Example: 120 applications at two new applications per second, each with two documents:

```bash
python kyc-data-seeder.py --base-url http://127.0.0.1:8000 \
  --count 120 --rate 2 --documents 2 --seed 42 \
  --mix 'clean=80,ocr_unavailable_2=10,screen_pep=10' \
  --output-dir ./seed-runs/morning-demo
```

Example: run for five minutes at one application every two seconds:

```bash
python kyc-data-seeder.py --base-url http://127.0.0.1:8000 \
  --duration 300 --rate 0.5 --documents-min 2 --documents-max 22 \
  --seed 42 --output-dir ./seed-runs/variable-docs
```

Example: reproduce an unfinished run:

```bash
python kyc-data-seeder.py --resume ./seed-runs/morning-demo
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--base-url` | http://127.0.0.1:8000 | Intake origin; required to match recorded origin on resume |
| `--count N` | — | Number of planned applications; mutually exclusive with duration |
| `--duration SECONDS` | — | Arrival window; exactly one of count/duration required for a new run unless scenario-plan supplies the count |
| `--rate R` | Required | Positive finite decimal applications per second |
| `--max-in-flight N` | 20 | Maximum concurrent application pipelines |
| `--documents N` | 2 | Fixed positive document count; exclusive with documents-min/max |
| `--documents-min N`, `--documents-max N` | — | Both required for a uniform inclusive count range |
| `--document-bytes N` | 4096 | Exact file size, including synthetic payload; must fit payload and intake limits |
| `--mix TEXT` | clean=100 | Comma-separated profile percentages totaling 100 |
| `--scenario-plan PATH` | — | Explicit per-application assignments; exclusive with mix |
| `--seed N` | 42 | Integer seed for reproducible data, assignments, and document counts |
| `--run-id ID` | Generated | Lowercase ASCII alphanumeric/hyphen run ID, 1–32 characters |
| `--output-dir PATH` | ./seed-runs/{run_id} | Local plan, checkpoint, and result files |
| `--draft-only` | false | Create and upload, but do not submit |
| `--dry-run` | false | Generate/validate plan and placeholder bytes; make no HTTP calls |
| `--request-timeout SECONDS` | 15 | Per-request total timeout |
| `--http-max-attempts N` | 5 | Total attempts per intake operation, including initial call |
| `--resume PATH` | — | Resume the saved plan; do not change payload/scenario options |

Validate arguments before contacting intake. Default output directories must not overwrite existing runs; require resume for an existing run directory. Document counts/sizes must be within the known intake limits (default 100 documents, 10 MiB/file, 100 MiB/application). Service-side limits remain authoritative.

## 3. Rate and concurrency semantics

Rate means **first create-request attempts for new logical applications per second**, not individual HTTP requests, successful creates, submissions, or workflow starts. Retries do not consume another logical arrival slot. Each application pipeline executes create → uploads in order → submit. Pipelines for different applications may overlap up to max-in-flight.

For count N, planned arrival offsets are i/R seconds for i=0…N−1. For duration D, plan arrivals where i/R < D, equivalent to ceil(D×R) applications. Use decimal arithmetic to avoid floating-point boundary errors. The first slot is at zero. The duration bounds admission of new work; admitted pipelines may drain after it expires.

Use a monotonic clock and paced admissions with no catch-up bursts. If capacity is unavailable, wait and record admission lag. After a late admission, schedule the next no earlier than 1/R seconds later. In duration mode, unadmitted slots remaining when the window closes are recorded as not admitted; do not send them later. In count mode, continue until every planned application is admitted, even if the run takes longer than its target schedule.

The seeder offers a target arrival rate, not a guarantee of workflow throughput. Report requested arrival rate, achieved create-attempt rate, achieved submission rate, admission lag, and capacity-limited time. Do not describe a 202/PENDING response as a confirmed workflow start.

## 4. Application and upload generation

Generate entirely synthetic applicant names, past dates of birth, US addresses, and example.com email addresses. Omit phone numbers by default. Do not use actual watchlist people to trigger scenarios; markers determine stub behavior.

Default two-document package: passport plus utility_bill. With one document, use passport only. For extra documents, cycle bank_statement, utility_bill, other. Additional uploads are distinct documents even if types repeat. Missing business evidence in a one-document package is not an intake error; the eventual processing rules decide its meaning.

Call these endpoints:

1. `POST /v1/applications`, JSON applicant + client_reference; capture application_id.
2. `POST /v1/applications/{application_id}/documents`, one multipart request per file with file, document_type, and client_document_reference; capture document_id.
3. Once every intended upload has succeeded, `POST /v1/applications/{application_id}/submit` with `{}`; capture submission_id, workflow_id, dispatch_status, and run_id if available.

For draft-only runs, stop after step 2. Never submit a partial upload package after an intake failure. Intake retry exhaustion leaves the application incomplete and is reported separately from an intentionally seeded processing failure.

## 5. Marker contract: transport without intake coupling

### Application marker

Set client_reference to this exact versioned grammar:

```text
ks1:{run_id}:a{ordinal}:s={screen_code}
```

Example: `ks1:morning-demo:a000001:s=pep`. Application ordinal is six decimal digits starting at 000001; v1 supports at most 999999 applications per run.

### Document marker

Set client_document_reference to:

```text
ks1:{run_id}:a{ordinal}:d{document_ordinal}:o={ocr_code}
```

Example: `ks1:morning-demo:a000001:d001:o=unavailable2`. Document ordinal is three decimal digits starting at 001. Both strings must fit the intake 128-character limits. Scenario codes are exact lowercase tokens from the tables below.

Intake stores the strings unchanged in application/document metadata and the frozen submission. It never branches on a ks1 prefix. Filenames, names, dates, and random bytes are not alternate scenario signals.

### How downstream callers obtain the markers

The workflow loads the frozen submission through an Activity. That package contains client_reference and each document's client_document_reference. The OCR-calling Activity forwards the document reference marker, application_id, document_id, blob reference, logical invocation ID, and attempt number to the future OCR stub. `screen_applicant` forwards client_reference, application_id, applicant information, logical invocation ID, and attempt number to the future screening stub.

Thus neither stub needs to query intake or infer scenarios from names. The OCR stub may read the synthetic document bytes for fixture facts. Screening reads its application marker directly; it must not depend on OCR succeeding to discover the marker. Whether screening is reached after an OCR failure is a workflow policy, outside this spec.

Recognized ks1 markers are parsed strictly by stubs. Malformed/unknown ks1 codes produce a fixture configuration error, never a silent success. Unmarked data behavior belongs in the later stub specs; this seeder always marks every application and document, including clean cases.

## 6. Synthetic document format

Upload a `.pdf` filename with media type application/pdf. These are deliberate non-PDF placeholders, not identity-document images, valid PDFs, or files requiring real OCR. Intake's opaque-byte upload contract permits this.

Bytes consist of UTF-8 magic line `KYC-SEED-FIXTURE-1`, one canonical compact JSON object on the next line, a newline, and deterministic ASCII padding to document-bytes. The JSON contains schema_version, run_id, application ordinal, document ordinal, document_type, and `fixture_fields`.

Example JSON content (pretty-printed for readability):

```json
{
  "schema_version": "1",
  "run_id": "morning-demo",
  "application_ordinal": 1,
  "document_ordinal": 1,
  "document_type": "passport",
  "fixture_fields": {
    "given_name": "Avery",
    "family_name": "Example",
    "date_of_birth": "1988-04-16",
    "document_number": "DEMO-000001-001",
    "expiry_date": "2030-04-16"
  }
}
```

Generate identity fields from declared information and address fields for address documents. Dates and any fixture mismatches are frozen in the plan. For `dob_mismatch`, alter the chosen document's fixture date of birth by one year, ensuring a valid past date, and keep declarations unchanged. The future OCR stub returns fixture_fields on a successful call; it must not replace them by copying declarations.

Fault behavior is selected solely by reference markers. Even a clean file is structurally not a PDF. Therefore `corrupt` means the stub returns its designated corrupt-document response; the stub cannot decide that all these uploads are corrupt by trying to parse them as PDFs. Reject document-bytes settings too small for any planned fixture before sending requests. Persist file SHA-256 and deterministically regenerate exact bytes for retries.

## 7. OCR codes: one document's behavior

| Code | Future stub behavior | Category |
| --- | --- | --- |
| `ok` | Return successful fixture_fields | Success |
| `corrupt` | HTTP 400, CORRUPT_PDF, on every attempt | Permanent document error |
| `unreadable` | HTTP 422, UNREADABLE_DOCUMENT, on every attempt | Permanent document error |
| `unavailable2` | HTTP 503 on attempts 1 and 2; success from attempt 3 | Recoverable service failure |
| `unavailable` | HTTP 503 on every attempt | Persistent service failure |
| `timeout2` | Exceed configured client timeout on attempts 1 and 2; success from attempt 3 | Recoverable timeout |
| `timeout` | Exceed configured client timeout on every attempt | Persistent timeout |
| `dob_mismatch` | Successful extraction with a deliberately different DOB | Business discrepancy |

The exact error body and timeout simulation mechanism will be defined in the OCR stub spec. The technical retry budget must allow at least three Activity attempts to demonstrate the recoverable profiles. A permanent error should be mapped to a non-retryable Activity error; business discrepancies are successful extraction results, not exceptions.

## 8. Screening codes: whole-application behavior

| Code | Future stub behavior | Category |
| --- | --- | --- |
| `clear` | Completed screening: no OFAC match and no PEP flag | Success |
| `ofac` | Completed screening with a synthetic potential OFAC match | Business finding |
| `pep` | Completed screening with a synthetic PEP flag | Business finding |
| `ofac_pep` | Completed screening with both flags | Business finding |
| `unavailable2` | HTTP 503 on attempts 1 and 2; completed clear result from attempt 3 | Recoverable service failure |
| `unavailable` | HTTP 503 on every attempt | Persistent service failure |
| `timeout2` | Exceed configured client timeout twice; completed clear result from attempt 3 | Recoverable timeout |
| `timeout` | Exceed configured client timeout on every attempt | Persistent timeout |

OFAC/PEP findings are completed business checks. They do not imply that the screening service failed, that the applicant is a confirmed sanctioned person, or that the workflow must automatically reject them. Review/disposition policy belongs in the workflow design.

### Repeatable attempt behavior

The calling Activity supplies its Temporal attempt number and a logical invocation ID stable across retries of that Activity, for example workflow_run_id + activity_id + document_id (document ID omitted for screening). Stubs base fail-first-two behavior on that explicit attempt number, not on request arrival order or an in-memory counter. Repeating the same invocation and attempt returns the same logical outcome. Disable hidden transport retries that would consume a scenario step; if an HTTP retry is necessary, retain the same attempt identity.

New runs have new application IDs and invocation identities. Replaying workflow history must not reissue stub calls. Activity retries can reissue calls with increased attempt numbers. Workflow resets or deliberately new Activity IDs are new invocation identities and may restart the scenario; they are not normal seeder resume operations.

## 9. Profiles, percentages, and explicit targeting

Built-in application profiles:

| Profile | OCR assignment | Screening assignment |
| --- | --- | --- |
| `clean` | All documents ok | clear |
| `ocr_corrupt` | Document 1 corrupt, others ok | clear |
| `ocr_unreadable` | Document 1 unreadable, others ok | clear |
| `ocr_unavailable_2` | Document 1 unavailable2, others ok | clear |
| `ocr_unavailable` | Document 1 unavailable, others ok | clear |
| `ocr_timeout_2` | Document 1 timeout2, others ok | clear |
| `ocr_timeout` | Document 1 timeout, others ok | clear |
| `ocr_dob_mismatch` | Document 1 dob_mismatch, others ok | clear |
| `screen_ofac` | All ok | ofac |
| `screen_pep` | All ok | pep |
| `screen_ofac_pep` | All ok | ofac_pep |
| `screen_unavailable_2` | All ok | unavailable2 |
| `screen_unavailable` | All ok | unavailable |
| `screen_timeout_2` | All ok | timeout2 |
| `screen_timeout` | All ok | timeout |
| `ocr_retry_and_pep` | Document 1 unavailable2, others ok | pep |

Mix percentages refer to **planned applications**, not documents, requests, or retries. Reject negative/nonfinite percentages, duplicate profile names, unknown profiles, and totals other than 100. Allocate integer counts with the largest-remainder method: floor each quota, assign remaining places by descending fractional remainder with lexical profile name as tie-breaker, then shuffle application assignments using the recorded seed. Display realized counts before HTTP traffic begins. For a tiny run, a positive percentage can round to zero.

The example 120-application mix produces exactly 96 clean applications, 12 with one transient OCR document, and 12 with PEP findings. With two documents per application, 12 of 240 documents are marked unavailable2. These are planned proportions; processing may never reach a stage, and intake failures may prevent some submissions. Report observed counts separately.

For exact targeting, accept a JSON scenario-plan file with one entry for every application ordinal. It determines application count and is incompatible with duration; a supplied count must equal the entry count. Entries are contiguous, contain document_count, screen_code, and optional per-document ocr_codes keyed by document ordinal (default ok). Document_count overrides document-count CLI options, which must be omitted. Validate indices against each document count.

```json
{
  "version": 1,
  "applications": [
    {
      "ordinal": 1,
      "document_count": 3,
      "screen_code": "pep",
      "ocr_codes": {"2": "corrupt"}
    },
    {
      "ordinal": 2,
      "document_count": 2,
      "screen_code": "unavailable2",
      "ocr_codes": {}
    }
  ]
}
```

This lets the operator mark a particular upload, mark a whole application, or combine scenarios intentionally. No additional distribution files or direct stub setup calls are necessary.

## 10. Reproducibility, retries, and resume

Generate and persist the complete plan before making HTTP calls: run ID, generator version, seed, reference date, effective configuration, ordered applications, applicant payloads, document fixture payloads/checksums, scenario assignments, and exact idempotency keys. Reference dates must not drift between retries or resume. Concurrency must not change scenario assignment or applicant data.

Key grammar: `ks1:{run_id}:a{ordinal}:create`, `ks1:{run_id}:a{ordinal}:d{document_ordinal}:upload`, and `ks1:{run_id}:a{ordinal}:submit`. Keys are <=128 characters. Run IDs must be unique within the same intake dataset. Seed alone does not make server-generated application/document IDs repeat; repeatability means identical planned inputs and scenarios.

Retry transport failures/timeouts and HTTP 408, 429, 500, 502, 503, 504 with the same payload and key, up to http-max-attempts. Use exponential backoff (initial 0.5 seconds, cap 10 seconds) with jitter and honor a valid bounded Retry-After (maximum 60 seconds). Do not retry other 4xx errors automatically. Jitter affects timing only, never generated test cases.

Create/upload accept 201 for new resources or 200 for idempotent replays. Submit accepts 202 with STARTED, PENDING, or BLOCKED; do not re-submit solely because processing has not started. BLOCKED is a recorded handoff problem, not an HTTP upload failure. Preserve response codes and request IDs for diagnosis.

After each acknowledged operation, atomically checkpoint its returned IDs and state. An interrupted/uncertain operation is retried with its original key on resume. Upload resumes use identical bytes, filename, type, and reference. Submit resumes cannot accidentally add documents after closing the application. Stop and report an inconsistent checkpoint instead of inventing new data.

Resume retains the configured rate and capacity limit, but begins a new pacing window for remaining application pipelines; it does not replay historical timing or produce a catch-up burst. Record segments separately. Duration-mode slots never admitted in the original segment stay not admitted; resume repairs only previously admitted incomplete pipelines. Dry-run output may later be resumed for execution as a count-style planned run.

First Ctrl-C stops admitting applications and gives in-flight pipelines up to 30 seconds to drain. Then cancel remaining requests and preserve uncertain operation state for idempotent resume. Never submit a partially uploaded application during shutdown.

## 11. Output and observability

Persist in output-dir:

- `plan.json`: immutable reproducibility plan and fixture payloads, including bytes generation algorithm version.
- `checkpoint.json`: atomic progress snapshot with operation states and assigned IDs.
- `events.jsonl`: time, application ordinal, operation, attempt, HTTP status, request ID, latency, and sanitized error code.
- `results.jsonl`: one current result per application generated from checkpoints at termination, including not-admitted and incomplete applications.
- `summary.json`: configuration, duration, rate/capacity metrics, planned/observed counts, and intake/handoff error totals.

Report counts of planned, admitted, created, document uploads committed, submitted, draft-only complete, incomplete, intake-failed, and not-admitted applications. Separate last-observed STARTED/PENDING/BLOCKED counts; these are receipt observations, not continuously polled workflow status.

Report planned profiles and document markers separately from observed downstream outcomes. This seeder does not connect to Temporal or processing Postgres to verify OCR behavior, screening results, or final decisions. It must never claim “12 OCR failures occurred” merely because 12 scenarios were assigned. Later workflow dashboards can join application_id or client_reference to the processing results.

Exit codes: 0 when all intended admitted operations succeed and there are no not-admitted slots or BLOCKED receipts; PENDING is accepted handoff and may still yield 0. Exit 1 for intake failures, incomplete work, capacity-dropped duration slots, or observed BLOCKED receipts. Exit 2 for invalid CLI/plan/configuration. Exit 130 for interrupted runs. Draft-only/dry-run success may return 0 without workflows; label the mode clearly.

## 12. Acceptance criteria

1. A dry run makes no network calls and emits a complete validated plan.
2. With a fake intake API, verify create/upload/submit order and that submit waits for every intended upload.
3. With controlled latency and a test clock, verify pacing, concurrency cap, no catch-up burst, duration cutoff, and reported admission lag.
4. A 120-application 80/10/10 mix has exact 96/12/12 allocations; changing concurrency leaves the plan unchanged.
5. Application and document markers round-trip through Garage metadata and the frozen submission without interpretation by intake.
6. Upload bytes are exact-sized, synthetic, deterministic, and accepted without PDF parsing. A document mismatch changes fixture data only, not declarations.
7. Lost create/upload responses and a process restart recover the same IDs through identical idempotency keys and bytes.
8. A permanently failed upload prevents submission of that application; other pipelines continue and failures appear in summary.
9. Explicit scenario plans can mark document 2 of a three-document application independently of its screening marker.
10. Intake-only integration runs require no OCR/screening services, processing Postgres, or direct Temporal access from the seeder.
11. When stubs/Activities are built, contract tests show unavailable2 failing attempts 1–2 and succeeding at 3, with duplicate same-attempt calls leaving the result unchanged. This is future integration acceptance, not a prerequisite for seeder implementation.
12. Screening PEP/OFAC responses are recorded as business findings, separately from service errors; persistent timeout cases stop according to worker retry policy, not seeder retry policy.

## 13. Relationship to the intake specification

Use intake spec version 1.2 or later, which explicitly preserves client_reference and client_document_reference in the submitted package. Its endpoint shapes and no-Postgres boundary remain unchanged. The only intake clarification is opaque reference preservation; all scenario vocabulary and behavior live here and in future stub contracts.

Implementation should use an asynchronous HTTP client and bounded application tasks. No implementation or live traffic is requested by this specification alone. Next work is the Python script, followed by stub API contracts that implement the marker and attempt conventions.
