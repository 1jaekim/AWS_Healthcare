"""Wire contracts shared by the reviewer and challenger A2A runtimes."""

from __future__ import annotations

from typing import Any


REQUEST_SCHEMA = "clinical-deliberation-request/v1"
RESPONSE_SCHEMA = "clinical-deliberation-review/v1"
ROLES = frozenset({"evidence_reviewer", "challenge_reviewer"})
RECOMMENDATIONS = frozenset({"OK", "NOT_OK", "UNKNOWN"})
MAX_CRITERIA = 5


class ContractError(ValueError):
    """The A2A payload does not satisfy the deliberation contract."""


def validate_request(payload: Any, *, role: str) -> dict[str, Any]:
    if role not in ROLES:
        raise ContractError(f"unsupported agent role: {role}")
    if not isinstance(payload, dict):
        raise ContractError("request must be a JSON object")
    if payload.get("schema") != REQUEST_SCHEMA:
        raise ContractError(f"schema must be {REQUEST_SCHEMA}")

    criteria = payload.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise ContractError("criteria must be a non-empty list")
    if len(criteria) > MAX_CRITERIA:
        raise ContractError(f"criteria may contain at most {MAX_CRITERIA} items")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in criteria:
        if not isinstance(raw, dict):
            raise ContractError("each criterion must be an object")
        criterion_id = str(raw.get("criterion_id", "")).strip()
        if not criterion_id or criterion_id in seen:
            raise ContractError("criterion_id must be present and unique")
        seen.add(criterion_id)
        item = dict(raw)
        item["criterion_id"] = criterion_id
        item["source_ids"] = [
            str(value) for value in raw.get("source_ids") or [] if str(value).strip()
        ]
        normalized.append(item)

    prior = payload.get("prior_review")
    if role == "challenge_reviewer" and not isinstance(prior, list):
        raise ContractError("challenge_reviewer requires prior_review")

    return {
        "schema": REQUEST_SCHEMA,
        "discussion_id": str(payload.get("discussion_id", "")).strip(),
        "run_id": str(payload.get("run_id", "")).strip(),
        "round": int(payload.get("round", 1) or 1),
        "criteria": normalized,
        "prior_review": prior if isinstance(prior, list) else [],
    }


def normalize_decisions(payload: Any, *, allowed_ids: set[str]) -> list[dict[str, Any]]:
    decisions = payload.get("decisions") if isinstance(payload, dict) else None
    if not isinstance(decisions, list):
        raise ContractError("model response must contain a decisions list")

    normalized: list[dict[str, Any]] = []
    for raw in decisions:
        if not isinstance(raw, dict):
            continue
        criterion_id = str(raw.get("criterion_id", "")).strip()
        if criterion_id not in allowed_ids:
            continue
        recommendation = str(raw.get("recommendation", "UNKNOWN")).upper()
        if recommendation not in RECOMMENDATIONS:
            recommendation = "UNKNOWN"
        rationale = str(raw.get("rationale", "")).strip()
        if recommendation in {"OK", "NOT_OK"} and not rationale:
            recommendation = "UNKNOWN"
        normalized.append(
            {
                "criterion_id": criterion_id,
                "recommendation": recommendation,
                "source_ids": [
                    str(value)
                    for value in raw.get("source_ids") or []
                    if str(value).strip()
                ],
                "rationale": rationale,
            }
        )
    return normalized
