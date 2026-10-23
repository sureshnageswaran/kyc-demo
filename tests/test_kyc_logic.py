from temporal import kyc_logic
from temporal.models import EvaluateInput, EvaluationDocument
from tests.fakes import policy_snapshot

DECLARED = {"given_name": "Avery", "family_name": "Example", "date_of_birth": "1988-04-16"}
NOW = "2026-10-23T00:00:00+00:00"


def run(fields):
    doc = EvaluationDocument("d1", "drivers_license", "SUCCEEDED", fields)
    return kyc_logic.evaluate(EvaluateInput("e1", 1, DECLARED, [doc], policy_snapshot(1, 48), NOW))


def types(result):
    return {f.finding_type for f in result.findings}


def test_expired_document_and_low_confidence_are_findings_but_metadata_is_not_a_fact():
    good = {"given_name": "Avery", "family_name": "Example", "date_of_birth": "1988-04-16"}
    ok = run({**good, "document_number": "D1", "expiration_date": "2030-04-12", "confidence": 0.95})
    assert not {"DOCUMENT_EXPIRED", "LOW_OCR_CONFIDENCE"} & types(ok)
    assert "expiration_date" not in ok.evaluated_facts
    assert "DOCUMENT_EXPIRED" in types(run({**good, "expiration_date": "2020-01-01"}))
    low = run({**good, "confidence": 0.42})
    assert "LOW_OCR_CONFIDENCE" in types(low) and low.review_required
