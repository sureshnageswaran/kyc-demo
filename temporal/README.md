# KYC Temporal worker

Implements `docs/kyc_workflow_and_postgres_spec.md`: `KycApplicationWorkflow` on task queue
`kyc-applications`, the 11 Activities, and the operator Query/Updates.

## Run

```bash
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt
$EDITOR .env                                  # fill S3 keys, stub URLs/API keys
sql/apply_schema.sh                           # idempotent; uses DATABASE_URL from .env
.venv/bin/python -m temporal.cli.publish_policy 1 # writes policy v1 to Garage and points current.json at it
.venv/bin/python -m temporal.worker           # also registers the Kyc* Search Attributes
```

Operate (demo stand-in for the authenticated review/operator API):

```bash
python -m temporal.cli.start_workflow --application-id A --submission-id S --sha256 H --timer-profile demo-v1
python -m temporal.cli.operator_cli state A
python -m temporal.cli.operator_cli decide A APPROVE --actor alice
python -m temporal.cli.publish_policy 2
python -m temporal.cli.operator_cli reevaluate A --version 2 --sha256 <hash printed by publish_policy> --actor ops
python -m temporal.cli.operator_cli retry A --actor ops
```

Tests: `KYC_TEST_DATABASE_URL=postgresql://.../scratch pytest` (DB tests skip without it; the schema
must already be applied there). The first workflow test run downloads Temporal's test server.

## Layout

```
temporal/
  worker.py            Worker entrypoint (also exports connect())
  config.py            Settings from .env (never imported by workflow code)
  models.py            Payload dataclasses (references and compact results only)
  errors.py            Error-type constants shared by activities and retry policies
  kyc_logic.py         Pure fact normalisation, rule evaluation, finding generation
  workflow/
    kyc_application.py Deterministic orchestration, review timers, Updates, Query, recovery waits
    timers.py          Frozen normal-v1 / demo-v1 timer profiles (never edit; add -v2)
  activities/          All I/O; one module per Activity, combined into KycActivities in __init__.py
    base.py            Shared deps (settings, pool, storage, http) and helpers
    start_processing.py record_event.py load_submission.py document_parse.py load_policy.py
    evaluate_application.py screen_applicant.py persist_assessment.py persist_final_outcome.py
    activate_account.py persist_activation.py
    repo.py            Transactional, idempotent SQL with in-transaction audit events
    storage.py         Garage (S3) client
  cli/                 start_workflow, operator_cli, publish_policy, search_attributes
```

## Stub HTTP contracts assumed (the stub specs don't exist yet)

- OCR `POST {OCR_STUB_URL}/v1/extract`, multipart `file` + form fields
  (`application_id, document_id, document_type, client_document_reference, invocation_id, attempt`).
  200 `{"fixture_fields": {...}}`; 400/422 = permanent document error; 5xx/timeout = retryable.
- Screening `POST {SCREENING_STUB_URL}/v1/screen` JSON
  (`application_id, client_reference, applicant, invocation_id, attempt`) ->
  `{"potential_ofac_match": bool, "pep_flag": bool, "details": {}, "provider_reference": "..."}`.
- Account `POST {ACCOUNT_SERVICE_URL}/v1/accounts`, header `Idempotency-Key: kyc-activate:{application_id}`
  -> `{"account_id": "...", "provider_reference": "...", "activated_at": "..."}`; any other 4xx
  (including 409 key reuse) = rejection -> `BLOCKED`.
- Optional `*_API_KEY` values are sent as `Authorization: Bearer ...`.
- `submission.json` is read tolerantly: `applicant` (or `declared_applicant`), `documents[]` per spec 5.2.
