"""Activity 3: app_document_parse."""
import hashlib

from temporalio import activity

from .. import errors
from ..models import DocumentOutcome, ParseDocumentInput
from .base import ActivityBase, error_code, utcnow  # STUB-TODO(OCR): re-add json_or_none


# STUB-TODO(OCR): simulated extraction results, keyed by document_type, matching the declared
# applicant (Avery Example) in data/submission.json so the happy path evaluates to PASS.
_ADDRESS = {"line1": "100 Example Lane", "city": "Metuchen", "region": "NJ", "postal_code": "08840"}
_SIMULATED_FIELDS = {
    "drivers_license": {"given_name": "Avery", "family_name": "Example", "date_of_birth": "1988-04-16",
                        "address": _ADDRESS},
    "utility_bill": {"given_name": "Avery", "family_name": "Example", "address": _ADDRESS},
    "other": {"average_credit_age_months": 72},  # credit report; policy v1 needs > 48
}


class DocumentParse(ActivityBase):
    @activity.defn(name="app_document_parse")
    async def app_document_parse(self, inp: ParseDocumentInput) -> DocumentOutcome:
        ref = inp.ref
        attempt = activity.info().attempt
        data: bytes | None = None
        try:
            data = await self.storage.get_bytes(ref.bucket, ref.object_key, self.cfg.max_document_bytes)
            if len(data) != ref.size_bytes or hashlib.sha256(data).hexdigest() != ref.sha256:
                raise errors.fatal(errors.DOCUMENT_INTEGRITY, "document size or checksum mismatch")
            # STUB-TODO(OCR): real OCR call disabled until the OCR stub exists. Restore it
            # and delete the simulated response below.
            # resp = await self._post(
            #     f"{self.cfg.ocr_url}/v1/extract", self.cfg.ocr_http_timeout, self.cfg.ocr_api_key, "OCR",
            #     files={"file": (ref.original_filename or "document", data, "application/octet-stream")},
            #     data={
            #         "application_id": ref.application_id, "document_id": ref.document_id,
            #         "document_type": ref.document_type,
            #         "client_document_reference": ref.client_document_reference or "",
            #         "invocation_id": inp.invocation_id, "attempt": str(attempt)})
            status_code = 200
            body = {"fixture_fields": _SIMULATED_FIELDS[ref.document_type]}
        finally:
            data = None  # release the only reference to the document bytes

        # STUB-TODO(OCR): with the real call restored, set status_code = resp.status_code and
        # body = json_or_none(resp) here (and move them out of the try block above).
        if status_code in (400, 422):
            default = "CORRUPT_PDF" if status_code == 400 else "UNREADABLE_DOCUMENT"
            return DocumentOutcome(
                document_id=ref.document_id, status="DOCUMENT_ERROR", attempts=attempt,
                error_code=error_code(body, default), error_message="OCR reported an unusable document")
        if status_code != 200 or not isinstance(body, dict):
            raise errors.transient(errors.SERVICE_UNAVAILABLE, f"unexpected OCR response ({status_code})")
        fields = body.get("fixture_fields") or body.get("extracted_fields") or body.get("fields")
        if not isinstance(fields, dict):
            raise errors.transient(errors.SERVICE_UNAVAILABLE, "OCR response had no extracted fields")
        return DocumentOutcome(document_id=ref.document_id, status="SUCCEEDED", extracted_fields=fields,
                               attempts=attempt, extracted_at=utcnow())
