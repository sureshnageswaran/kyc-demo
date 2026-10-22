"""Shared dependencies and helpers for the Activity mixins.

Each Activity lives in its own module as a mixin over `ActivityBase`;
`KycActivities` (see `__init__.py`) combines them into the one object whose
bound methods are registered with the worker.
"""
from datetime import datetime, timezone
from typing import Any

import httpx
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .. import errors
from ..config import Settings
from .storage import Storage


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def error_code(body: Any, default: str) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("code"):
            return str(err["code"])[:100]
        if isinstance(err, str):
            return err[:100]
        if body.get("code"):
            return str(body["code"])[:100]
    return default


def json_or_none(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


class ActivityBase:
    def __init__(self, settings: Settings, pool: AsyncConnectionPool, storage: Storage,
                 http: httpx.AsyncClient | None = None):
        self.cfg = settings
        self.pool = pool
        self.storage = storage
        self.http = http or httpx.AsyncClient()

    async def _tx(self, fn, *args):
        async with self.pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    return await fn(cur, *args)

    @staticmethod
    def _auth(api_key: str | None) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def _post(self, url: str, timeout: float, api_key: str | None, what: str, **kwargs) -> httpx.Response:
        try:
            resp = await self.http.post(url, timeout=timeout, headers={
                **self._auth(api_key), **kwargs.pop("headers", {})}, **kwargs)
        except httpx.TimeoutException:
            raise errors.transient(errors.SERVICE_UNAVAILABLE, f"{what} timed out")
        except httpx.TransportError as exc:
            raise errors.transient(errors.SERVICE_UNAVAILABLE, f"{what} unreachable: {type(exc).__name__}")
        if resp.status_code in (401, 403, 404):
            raise errors.fatal(errors.CONFIGURATION, f"{what} rejected the request ({resp.status_code})")
        if resp.status_code in (408, 425, 429) or resp.status_code >= 500:
            raise errors.transient(errors.SERVICE_UNAVAILABLE, f"{what} unavailable ({resp.status_code})")
        return resp
