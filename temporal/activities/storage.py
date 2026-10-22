"""Garage (S3-compatible) access. Blocking boto3 calls, run via to_thread."""
import asyncio
import hashlib
import json
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .. import errors
from ..config import Settings


class Storage:
    def __init__(self, settings: Settings):
        # No SDK-level retries: Temporal owns retries. Timeouts leave headroom
        # inside the Activity start-to-close budget.
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            region_name=settings.s3_region,
            config=Config(
                s3={"addressing_style": settings.s3_addressing_style},
                retries={"max_attempts": 1},
                connect_timeout=3,
                read_timeout=10,
            ),
        )

    def _get(self, bucket: str, key: str, max_bytes: int | None) -> bytes:
        try:
            resp = self._client.get_object(Bucket=bucket, Key=key)
            body = resp["Body"]
            try:
                data = body.read(max_bytes + 1) if max_bytes else body.read()
            finally:
                body.close()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "NoSuchBucket", "404"):
                raise errors.fatal(errors.DOCUMENT_INTEGRITY, f"object not found: {bucket}/{key}")
            if code in ("AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"):
                raise errors.fatal(errors.CONFIGURATION, f"storage rejected credentials ({code})")
            raise errors.transient(errors.SERVICE_UNAVAILABLE, f"storage error ({code})")
        except BotoCoreError as exc:
            raise errors.transient(errors.SERVICE_UNAVAILABLE, f"storage unreachable: {type(exc).__name__}")
        if max_bytes and len(data) > max_bytes:
            raise errors.fatal(errors.DOCUMENT_INTEGRITY, "object exceeds maximum size")
        return data

    async def get_bytes(self, bucket: str, key: str, max_bytes: int | None = None) -> bytes:
        return await asyncio.to_thread(self._get, bucket, key, max_bytes)

    async def get_json(self, bucket: str, key: str) -> tuple[Any, str]:
        """Return (parsed JSON, sha256 of the exact stored bytes)."""
        data = await self.get_bytes(bucket, key, 5 * 1024 * 1024)
        try:
            return json.loads(data), hashlib.sha256(data).hexdigest()
        except ValueError:
            raise errors.fatal(errors.SUBMISSION_INVALID, f"{key} is not valid JSON")

    def put_bytes(self, bucket: str, key: str, data: bytes, content_type: str = "application/json") -> None:
        self._client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
