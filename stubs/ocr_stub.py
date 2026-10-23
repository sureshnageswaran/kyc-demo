"""Barebones OCR stub: uvicorn stubs.ocr_stub:app --port 8101

Returns canned data from metadata.scenario (default "valid_id"); it never reads a document.
Data matches the demo applicant (Avery Example) in data/submission.json.
"""
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI(title="ocr stub")

_ADDRESS = {"line1": "100 Example Lane", "city": "Metuchen", "region": "NJ", "postal_code": "08840"}
_IDENTITY = {"full_name": "Avery Example", "date_of_birth": "1988-04-16", "document_number": "D1234567",
             "expiration_date": "2030-04-12"}
_ADDRESS_PROOF = {"full_name": "Avery Example", "address": _ADDRESS}
_CREDIT_REPORT = {"average_credit_age_months": 72}
_CANNED = {"drivers_license": _IDENTITY, "passport": _IDENTITY, "national_id": _IDENTITY,
           "government_id": _IDENTITY, "utility_bill": _ADDRESS_PROOF, "bank_statement": _ADDRESS_PROOF}


class ExtractRequest(BaseModel):
    application_id: str
    document_id: str
    document_type: str = "other"
    metadata: dict[str, Any] = Field(default_factory=dict)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/extract")
def extract(req: ExtractRequest) -> dict:
    scenario = req.metadata.get("scenario", "valid_id")
    out = {"application_id": req.application_id, "document_id": req.document_id}
    if scenario == "unreadable":
        return {**out, "status": "failed", "reason_code": "UNREADABLE_DOCUMENT"}
    data = dict(_CANNED.get(req.document_type, _CREDIT_REPORT))
    if scenario == "expired_id":
        data["expiration_date"] = "2020-01-01"
    elif scenario == "name_mismatch" and "full_name" in data:
        data["full_name"] = "Morgan Mismatch"
    out.update(status="completed", extracted_data=data)
    if scenario == "low_confidence":
        out["confidence"] = 0.42
    return out
