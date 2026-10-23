"""Activity 3: app_document_parse."""
import hashlib

from temporalio import activity

from .. import errors
from ..models import DocumentOutcome, ParseDocumentInput
from .base import ActivityBase, json_or_none, utcnow


def _to_fields(data: dict, confidence) -> dict:
    """Map the OCR response onto the field names evaluation uses (full_name -> given/family name)."""
    fields = dict(data)
    full_name = fields.pop("full_name", None)
    if full_name:
        given, _, family = str(full_name).partition(" ")
        fields.update(given_name=given, family_name=family)
    if confidence is not None:
        fields["confidence"] = confidence
    return fields


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
            resp = await self._post(
                f"{self.cfg.ocr_url}/v1/extract", self.cfg.ocr_http_timeout, self.cfg.ocr_api_key, "OCR",
                json={"application_id": ref.application_id, "document_id": ref.document_id,
                      "document_type": ref.document_type,
                      # Demo only: the submission's metadata packet tells the stub which scenario to return.
                      "metadata": ref.stub_metadata or {}})
            body = json_or_none(resp)
        finally:
            data = None  # release the only reference to the document bytes

        if resp.status_code != 200 or not isinstance(body, dict) or body.get("status") not in ("completed", "failed"):
            raise errors.transient(errors.SERVICE_UNAVAILABLE, f"unexpected OCR response ({resp.status_code})")
        if body["status"] == "failed":
            return DocumentOutcome(
                document_id=ref.document_id, status="DOCUMENT_ERROR", attempts=attempt,
                error_code=str(body.get("reason_code") or "UNREADABLE_DOCUMENT")[:100],
                error_message="OCR reported an unusable document")
        data = body.get("extracted_data")
        if not isinstance(data, dict):
            raise errors.transient(errors.SERVICE_UNAVAILABLE, "OCR response had no extracted data")
        return DocumentOutcome(document_id=ref.document_id, status="SUCCEEDED",
                               extracted_fields=_to_fields(data, body.get("confidence")),
                               attempts=attempt, extracted_at=utcnow())
