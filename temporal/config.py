"""Worker/activity configuration, loaded from the environment (.env).

Workflow code must never import this module's values: business timer
constants are frozen in `timers.py` and selected by workflow input, so replay
does not depend on mutable worker environment settings.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent


def _opt(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _req(name: str) -> str:
    value = _opt(name)
    if value is None:
        raise RuntimeError(f"{name} is not set; populate it in {REPO_ROOT / '.env'}")
    return value


@dataclass(frozen=True)
class Settings:
    # Temporal
    temporal_address: str
    temporal_namespace: str
    temporal_task_queue: str
    temporal_api_key: str | None
    temporal_tls: bool
    # Worker capacity
    max_concurrent_activities: int
    max_concurrent_workflow_tasks: int
    # Business Postgres
    database_url: str
    db_pool_max: int
    # Garage / S3
    s3_endpoint_url: str
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_region: str
    s3_bucket: str
    s3_addressing_style: str
    # Policy
    policy_bucket: str
    # Service stubs / providers
    ocr_url: str
    ocr_api_key: str | None
    screening_url: str
    screening_api_key: str | None
    account_url: str
    account_api_key: str | None
    # HTTP timeouts: must sit below the Activity start-to-close timeouts.
    ocr_http_timeout: float
    screening_http_timeout: float
    account_http_timeout: float
    # Misc
    max_document_bytes: int

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        load_dotenv(env_file or REPO_ROOT / ".env")
        bucket = _req("S3_BUCKET")
        return cls(
            temporal_address=_opt("TEMPORAL_ADDRESS", "localhost:7233"),
            temporal_namespace=_opt("TEMPORAL_NAMESPACE", "default"),
            temporal_task_queue=_opt("TEMPORAL_TASK_QUEUE", "kyc-applications"),
            temporal_api_key=_opt("TEMPORAL_API_KEY"),
            temporal_tls=_opt("TEMPORAL_TLS", "false").lower() == "true",
            max_concurrent_activities=int(_opt("WORKER_MAX_CONCURRENT_ACTIVITIES", "50")),
            max_concurrent_workflow_tasks=int(_opt("WORKER_MAX_CONCURRENT_WORKFLOW_TASKS", "100")),
            database_url=_req("DATABASE_URL"),
            db_pool_max=int(_opt("DB_POOL_MAX", "10")),
            s3_endpoint_url=_req("S3_ENDPOINT_URL"),
            s3_access_key_id=_req("S3_ACCESS_KEY_ID"),
            s3_secret_access_key=_req("S3_SECRET_ACCESS_KEY"),
            s3_region=_opt("S3_REGION", "garage"),
            s3_bucket=bucket,
            s3_addressing_style=_opt("S3_ADDRESSING_STYLE", "path"),
            policy_bucket=_opt("POLICY_BUCKET", bucket),
            ocr_url=_req("OCR_STUB_URL").rstrip("/"),
            ocr_api_key=_opt("OCR_API_KEY"),
            screening_url=_req("SCREENING_STUB_URL").rstrip("/"),
            screening_api_key=_opt("SCREENING_API_KEY"),
            account_url=_req("ACCOUNT_SERVICE_URL").rstrip("/"),
            account_api_key=_opt("ACCOUNT_API_KEY"),
            ocr_http_timeout=float(_opt("OCR_HTTP_TIMEOUT_SECONDS", "30")),
            screening_http_timeout=float(_opt("SCREENING_HTTP_TIMEOUT_SECONDS", "15")),
            account_http_timeout=float(_opt("ACCOUNT_HTTP_TIMEOUT_SECONDS", "15")),
            max_document_bytes=int(_opt("MAX_DOCUMENT_BYTES", "10485760")),
        )
