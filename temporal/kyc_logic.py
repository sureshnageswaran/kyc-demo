"""Pure, deterministic business logic: no I/O, no clocks, no randomness.

Imported by both the evaluation Activity and (for disposition helpers) the
workflow, so it must stay free of side effects.
"""
import re
from datetime import date
from typing import Any

from .models import (
    EvaluateInput,
    EvaluationDocument,
    EvaluationResult,
    Finding,
    ScreeningResult,
)

IDENTITY_TYPES = {"passport", "drivers_license", "national_id"}
ADDRESS_TYPES = {"utility_bill", "bank_statement"}

OPERATORS = {
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
}
# REVIEW sends the application to a human; NONE records the outcome only.
DISPOSITIONS = {"REVIEW", "NONE"}

_ADDRESS_FIELDS = ("line1", "city", "region", "postal_code")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PolicyError(ValueError):
    """The policy JSON is structurally invalid."""


def validate_policy(policy: dict[str, Any], expected_id: str | None = None,
                    expected_version: int | None = None) -> None:
    if policy.get("schema_version") != "1.0":
        raise PolicyError("unsupported policy schema_version")
    if expected_id is not None and policy.get("policy_id") != expected_id:
        raise PolicyError("policy_id mismatch")
    if expected_version is not None and policy.get("version") != expected_version:
        raise PolicyError("policy version mismatch")
    rules = policy.get("rules")
    if not isinstance(rules, list):
        raise PolicyError("policy.rules must be a list")
    seen: set[str] = set()
    for rule in rules:
        rid = rule.get("rule_id")
        if not rid or rid in seen:
            raise PolicyError(f"missing or duplicate rule_id: {rid!r}")
        seen.add(rid)
        if rule.get("operator") not in OPERATORS:
            raise PolicyError(f"{rid}: unsupported operator {rule.get('operator')!r}")
        if not rule.get("field"):
            raise PolicyError(f"{rid}: field is required")
        if not isinstance(rule.get("threshold"), (int, float)) or isinstance(rule.get("threshold"), bool):
            raise PolicyError(f"{rid}: threshold must be numeric")
        for key in ("on_failure", "on_missing", "on_conflict"):
            if rule.get(key, "REVIEW") not in DISPOSITIONS:
                raise PolicyError(f"{rid}: {key} must be one of {sorted(DISPOSITIONS)}")


def is_sha256(value: str | None) -> bool:
    return bool(value and _SHA256.match(value))


# ---- fact normalisation -----------------------------------------------------


def _norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def _norm_date(value: Any) -> str:
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError:
        return _norm_text(value)


def _norm_number(value: Any) -> float | int | str:
    if isinstance(value, bool):
        return _norm_text(value)
    if isinstance(value, (int, float)):
        return value
    try:
        num = float(str(value).strip())
        return int(num) if num.is_integer() else num
    except ValueError:
        return _norm_text(value)


def _normalise(field_name: str, value: Any) -> Any:
    if field_name == "date_of_birth":
        return _norm_date(value)
    if field_name.endswith("_months") or field_name.endswith("_count"):
        return _norm_number(value)
    return _norm_text(value)


def _doc_facts(doc: EvaluationDocument) -> dict[str, Any]:
    fields = doc.extracted_fields or {}
    facts = {k: v for k, v in fields.items() if k not in ("address",) and not isinstance(v, (dict, list))}
    address = fields.get("address")
    if isinstance(address, dict):
        facts.update({k: address[k] for k in _ADDRESS_FIELDS if k in address})
    for k in _ADDRESS_FIELDS:  # tolerate "address_line1"-style fixture keys
        alt = f"address_{k}"
        if alt in fields and k not in facts:
            facts[k] = fields[alt]
    return facts


def collect_facts(documents: list[EvaluationDocument]) -> dict[str, Any]:
    """Return {field: {value, status, observations}}; status is OK or CONFLICT."""
    observed: dict[str, list[dict[str, Any]]] = {}
    for doc in sorted(documents, key=lambda d: d.document_id):
        if doc.status != "SUCCEEDED":
            continue
        for name, raw in _doc_facts(doc).items():
            if raw is None or raw == "":
                continue
            observed.setdefault(name, []).append(
                {"document_id": doc.document_id, "document_type": doc.document_type,
                 "raw": raw, "normalised": _normalise(name, raw)})
    facts: dict[str, Any] = {}
    for name, obs in observed.items():
        distinct = {repr(o["normalised"]) for o in obs}
        facts[name] = {
            "status": "OK" if len(distinct) == 1 else "CONFLICT",
            "value": obs[0]["normalised"] if len(distinct) == 1 else None,
            "observations": obs,
        }
    return facts


# ---- evaluation -------------------------------------------------------------


def _declared_values(declared: dict[str, Any]) -> dict[str, Any]:
    out = {k: declared[k] for k in ("given_name", "family_name", "date_of_birth") if k in declared}
    addr = declared.get("residential_address") or {}
    out.update({k: addr[k] for k in _ADDRESS_FIELDS if k in addr})
    return out


def evaluate(inp: EvaluateInput) -> EvaluationResult:
    policy = inp.policy.policy
    docs = inp.documents
    facts = collect_facts(docs)
    findings: list[Finding] = []
    review = False

    # Document-level issues.
    for doc in sorted(docs, key=lambda d: d.document_id):
        if doc.status in ("DOCUMENT_ERROR", "TECHNICAL_ERROR"):
            review = True
            findings.append(Finding(
                finding_key=f"doc:{doc.document_id}:{doc.status}",
                source="DOCUMENT", finding_type="DOCUMENT_UNUSABLE", severity="WARNING",
                document_id=doc.document_id,
                description=f"{doc.document_type} could not be used as evidence ({doc.status}).",
                evidence={"document_type": doc.document_type, "status": doc.status}))

    # Required evidence.
    good = [d for d in docs if d.status == "SUCCEEDED"]
    if not any(d.document_type in IDENTITY_TYPES for d in good):
        review = True
        findings.append(Finding(
            finding_key="evidence:identity_missing", source="DOCUMENT",
            finding_type="MISSING_IDENTITY_DOCUMENT", severity="WARNING",
            description="No successfully extracted identity document."))
    if not any(d.document_type in ADDRESS_TYPES for d in good):
        review = True
        findings.append(Finding(
            finding_key="evidence:address_missing", source="DOCUMENT",
            finding_type="MISSING_ADDRESS_PROOF", severity="WARNING",
            description="No successfully extracted proof-of-address document."))

    # Conflicts between documents and mismatches against declarations.
    declared = _declared_values(inp.declared_applicant)
    for name in sorted(facts):
        fact = facts[name]
        if fact["status"] == "CONFLICT":
            review = True
            findings.append(Finding(
                finding_key=f"conflict:{name}", source="DOCUMENT", finding_type="CONFLICTING_FACT",
                severity="WARNING", description=f"Documents disagree on {name}.",
                evidence={"field": name, "document_ids": [o["document_id"] for o in fact["observations"]]}))
        elif name in declared:
            declared_norm = _normalise(name, declared[name])
            if fact["value"] != declared_norm:
                review = True
                ftype = "DOB_MISMATCH" if name == "date_of_birth" else "DECLARATION_MISMATCH"
                findings.append(Finding(
                    finding_key=f"mismatch:{name}", source="DOCUMENT", finding_type=ftype,
                    severity="WARNING", description=f"Extracted {name} differs from the declaration.",
                    document_id=fact["observations"][0]["document_id"],
                    evidence={"field": name, "document_ids": [o["document_id"] for o in fact["observations"]]}))

    # Policy rules.
    rule_results: list[dict[str, Any]] = []
    for rule in policy["rules"]:
        fact = facts.get(rule["field"])
        if fact is None:
            outcome, observed, disp_key = "MISSING", None, "on_missing"
        elif fact["status"] == "CONFLICT":
            outcome, observed, disp_key = "CONFLICT", None, "on_conflict"
        else:
            observed = fact["value"]
            try:
                passed = OPERATORS[rule["operator"]](observed, rule["threshold"])
            except TypeError:  # non-numeric value against numeric threshold
                passed = False
            outcome, disp_key = ("PASS", None) if passed else ("FAIL", "on_failure")
        disposition = "NONE" if disp_key is None else rule.get(disp_key, "REVIEW")
        rule_results.append({
            "rule_id": rule["rule_id"], "field": rule["field"], "observed_value": observed,
            "operator": rule["operator"], "threshold": rule["threshold"],
            "outcome": outcome, "disposition": disposition})
        if outcome != "PASS" and disposition == "REVIEW":
            review = True
            findings.append(Finding(
                finding_key=f"rule:{rule['rule_id']}", source="POLICY",
                finding_type=f"RULE_{outcome}", severity="WARNING", rule_id=rule["rule_id"],
                description=(f"Rule {rule['rule_id']}: {rule['field']} {rule['operator']} "
                             f"{rule['threshold']} -> {outcome} (observed {observed!r})."),
                evidence={"observed_value": observed, "threshold": rule["threshold"],
                          "policy_version": inp.policy.version}))

    return EvaluationResult(
        evaluation_id=inp.evaluation_id,
        evaluation_sequence=inp.evaluation_sequence,
        evaluated_facts={k: {"status": v["status"], "value": v["value"],
                             "document_ids": [o["document_id"] for o in v["observations"]]}
                         for k, v in facts.items()},
        rule_results=rule_results,
        findings=findings,
        evaluation_outcome="REVIEW" if review else "PASS",
        review_required=review,
        evaluated_at=inp.evaluated_at,
        policy=inp.policy,
    )


# ---- screening disposition --------------------------------------------------


def screening_findings(screening: ScreeningResult) -> list[Finding]:
    """OFAC/PEP hits are business findings, never service failures."""
    out: list[Finding] = []
    details = screening.result_details or {}
    if screening.potential_ofac_match:
        out.append(Finding(
            finding_key="screening:ofac", source="SCREENING", finding_type="POTENTIAL_OFAC_MATCH",
            severity="BLOCKING", description="Screening reported a potential OFAC match.",
            evidence={"provider_reference": screening.provider_reference, **details}))
    if screening.pep_flag:
        out.append(Finding(
            finding_key="screening:pep", source="SCREENING", finding_type="PEP_FLAG",
            severity="WARNING", description="Screening flagged the applicant as a PEP.",
            evidence={"provider_reference": screening.provider_reference, **details}))
    return out


def screening_requires_review(screening: ScreeningResult | None) -> bool:
    return bool(screening and (screening.potential_ofac_match or screening.pep_flag))


def review_reason(findings: list[Finding]) -> str:
    types = sorted({f.finding_type for f in findings if f.severity != "INFO"})
    return ", ".join(types) if types else "REVIEW_REQUIRED"
