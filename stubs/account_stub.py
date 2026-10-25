"""Barebones account-service stub: uvicorn stubs.account_stub:app --port 8103

Creates an account immediately from metadata.scenario: "created" (default), "rejected" (409) or
"unavailable" (503). Stateless: the account id is derived from the application id, so repeating a
request with the same Idempotency-Key returns the same account.
"""
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="account stub")


class AccountRequest(BaseModel):
    application_id: str
    evaluation_id: str | None = None
    decision: str = "APPROVED"
    metadata: dict[str, Any] = Field(default_factory=dict)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/accounts")
def create_account(req: AccountRequest, idempotency_key: str | None = Header(default=None)):
    scenario = req.metadata.get("scenario", "created")
    if scenario == "rejected":
        return JSONResponse(status_code=409, content={
            "error": {"code": "ACCOUNT_REJECTED", "message": "Synthetic account rejection."}})
    if scenario == "unavailable":
        return JSONResponse(status_code=503, content={
            "error": {"code": "SERVICE_UNAVAILABLE", "message": "Synthetic outage."}})
    return JSONResponse(status_code=201, content={
        "account_id": f"acct-{req.application_id}", "provider_reference": f"stub-{idempotency_key or 'none'}",
        "activated_at": datetime.now(timezone.utc).isoformat()})
