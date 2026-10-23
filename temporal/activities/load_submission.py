"""Activity 2: app_load_submission."""
from temporalio import activity

from .. import errors, kyc_logic
from ..models import DocumentRef, LoadedSubmission, LoadSubmissionInput
from .base import ActivityBase, utcnow


class LoadSubmission(ActivityBase):
    @activity.defn(name="app_load_submission")
    async def app_load_submission(self, inp: LoadSubmissionInput) -> LoadedSubmission:
        ref = inp.submission_ref
        data, digest = await self.storage.get_json(ref.bucket, ref.key)
        if digest != ref.sha256:
            raise errors.fatal(errors.SUBMISSION_INVALID, "submission hash does not match the reference")
        if not isinstance(data, dict) or not str(data.get("schema_version", "")).startswith("1."):
            raise errors.fatal(errors.SUBMISSION_INVALID, "unsupported submission schema_version")
        if data.get("application_id") != inp.application_id or data.get("submission_id") != inp.submission_id:
            raise errors.fatal(errors.SUBMISSION_INVALID, "submission identity does not match the workflow input")
        applicant = data.get("applicant") or data.get("declared_applicant")
        if not isinstance(applicant, dict):
            raise errors.fatal(errors.SUBMISSION_INVALID, "submission has no declared applicant")

        packet = data.get("metadata_packet")
        packet = packet if isinstance(packet, dict) else {}
        ocr_meta = packet.get("ocr") if isinstance(packet.get("ocr"), dict) else {}
        refs: list[DocumentRef] = []
        seen: set[str] = set()
        for d in data.get("documents") or []:
            doc_id = d.get("document_id")
            key = d.get("object_key") or d.get("key")
            if not doc_id or doc_id in seen:
                raise errors.fatal(errors.SUBMISSION_INVALID, "missing or duplicate document_id")
            seen.add(doc_id)
            if d.get("application_id", inp.application_id) != inp.application_id or not key or \
                    not key.startswith(f"applications/{inp.application_id}/documents/{doc_id}/"):
                raise errors.fatal(errors.SUBMISSION_INVALID, f"document {doc_id} does not belong to this application")
            if not kyc_logic.is_sha256(d.get("sha256")) or not isinstance(d.get("size_bytes"), int) \
                    or d["size_bytes"] <= 0:
                raise errors.fatal(errors.SUBMISSION_INVALID, f"document {doc_id} has an invalid size or checksum")
            refs.append(DocumentRef(
                application_id=inp.application_id, document_id=doc_id,
                document_type=d.get("document_type", "other"),
                bucket=d.get("bucket") or d.get("object_bucket") or ref.bucket, object_key=key,
                size_bytes=d["size_bytes"], sha256=d["sha256"],
                client_document_reference=d.get("client_document_reference"),
                original_filename=d.get("original_filename"),
                stub_metadata=ocr_meta.get(doc_id) or ocr_meta.get(d.get("document_type", "other"))))
        return LoadedSubmission(
            application_id=inp.application_id, submission_id=inp.submission_id,
            submitted_at=data.get("submitted_at") or utcnow(), declared_applicant=applicant,
            client_reference=data.get("client_reference") or applicant.get("client_reference"),
            metadata_packet=packet or None,
            documents=sorted(refs, key=lambda r: r.document_id))
