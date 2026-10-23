"""Barebones OFAC/PEP screening stub: uvicorn stubs.screening_stub:app --port 8102

Decides immediately from metadata.scenario ("pass" by default, or "fail"). No state.
"""
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI(title="screening stub")


class ScreenRequest(BaseModel):
    application_id: str
    subject: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/screen")
def screen(req: ScreenRequest) -> dict:
    if req.metadata.get("scenario", "pass") == "fail":
        return {"application_id": req.application_id, "status": "review", "decision": "fail",
                "reason_code": "SYNTHETIC_MATCH", "message": "Synthetic screening match returned."}
    return {"application_id": req.application_id, "status": "clear", "decision": "pass",
            "reason_code": "NO_MATCH", "message": "No synthetic match found."}
