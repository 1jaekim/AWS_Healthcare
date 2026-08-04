"""Build de-identified documents for Amazon Bedrock Knowledge Bases GraphRAG."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Any, Iterable


TRIAL_VERDICT_PATTERN = re.compile(
    r"(?:임상시험\s*판정|clinical\s*trial\s*(?:verdict|decision))\s*:.*$",
    re.IGNORECASE | re.DOTALL,
)

PHI_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PERSON_NAME", re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}\b")),
    (
        "PERSON_NAME",
        re.compile(r"(?:(?:환자명|환자|성명|이름)\s*[:：]\s*)[가-힣]{2,5}"),
    ),
    (
        "DATE",
        re.compile(r"\b(?:\d{4}[./-]\d{1,2}[./-]\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\b"),
    ),
    (
        "PHONE",
        re.compile(r"\b(?:\+?82[-.\s]?)?0?1[016789][-.\s]?\d{3,4}[-.\s]?\d{4}\b"),
    ),
    ("PHONE", re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("RRN", re.compile(r"\b\d{6}-?[1-4]\d{6}\b")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    (
        "MRN",
        re.compile(r"\b(?:MRN|medical\s+record\s+number|환자번호|등록번호)\s*[:：]?\s*[A-Za-z0-9-]{4,}\b", re.IGNORECASE),
    ),
    (
        "ADDRESS",
        re.compile(r"\b\d{1,5}\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+(?:St|Ave|Blvd|Dr|Rd|Ln|Way|Ct)\b"),
    ),
    ("AGE", re.compile(r"\b(?:9\d|1\d{2})\s*세\b")),
)


@dataclass(frozen=True)
class GraphRagDocument:
    """A source document and its Bedrock metadata sidecar."""

    patient_key: str
    body: str
    metadata: dict[str, Any]
    note_count: int


def pseudonymize_identifier(prefix: str, identifier: str, secret: str) -> str:
    """Return a stable, non-reversible identifier suitable for retrieval filters."""

    if not identifier.strip():
        raise ValueError("identifier must not be empty")
    if not secret:
        raise ValueError("pseudonymization secret must not be empty")
    digest = hmac.new(
        secret.encode("utf-8"), identifier.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{prefix}_{digest[:24]}"


def remove_trial_verdict(text: str) -> str:
    """Remove generated eligibility labels while preserving clinical evidence."""

    return TRIAL_VERDICT_PATTERN.sub("", text).rstrip()


def mask_phi(text: str) -> str:
    """Mask common direct identifiers before a document can enter GraphRAG."""

    masked = text
    for phi_type, pattern in PHI_PATTERNS:
        masked = pattern.sub(f"[{phi_type}]", masked)
    return masked


def _source_text(note: dict[str, Any]) -> str:
    return str(note.get("text") or note.get("free_text_emr") or "").strip()


def _source_patient_id(note: dict[str, Any]) -> str:
    value = note.get("patient_key") or note.get("patient_id") or note.get("person_id")
    return "" if value is None else str(value).strip()


def _source_event_id(note: dict[str, Any]) -> str:
    value = note.get("event_key") or note.get("encounter_id") or note.get("note_id")
    return "" if value is None else str(value).strip()


def _is_canonical(note: dict[str, Any]) -> bool:
    flag = note.get("canonical_flag")
    if flag is None:
        return True
    return flag in (1, "1", True)


def _metadata_attribute(value: str, *, include_for_embedding: bool) -> dict[str, Any]:
    return {
        "value": {"type": "STRING", "stringValue": value},
        "includeForEmbedding": include_for_embedding,
    }


def build_patient_documents(
    notes: Iterable[dict[str, Any]], *, pseudonymization_secret: str
) -> list[GraphRagDocument]:
    """Aggregate canonical notes into one supported Markdown file per patient."""

    grouped: dict[str, list[tuple[str, dict[str, Any], str]]] = {}
    seen_note_ids: set[str] = set()

    for note in notes:
        if not _is_canonical(note):
            continue

        raw_text = _source_text(note)
        raw_patient_id = _source_patient_id(note)
        if len(raw_text) < 10 or not raw_patient_id:
            continue

        note_id = str(note.get("note_id") or "").strip()
        if note_id and note_id in seen_note_ids:
            continue
        if note_id:
            seen_note_ids.add(note_id)

        patient_key = (
            raw_patient_id
            if raw_patient_id.startswith("pt_")
            else pseudonymize_identifier("pt", raw_patient_id, pseudonymization_secret)
        )
        raw_event_id = _source_event_id(note) or note_id
        event_key = (
            raw_event_id
            if raw_event_id.startswith("evt_")
            else pseudonymize_identifier(
                "evt", f"{raw_patient_id}:{raw_event_id}", pseudonymization_secret
            )
        )
        cleaned = mask_phi(remove_trial_verdict(raw_text))
        if len(cleaned) < 10:
            continue
        grouped.setdefault(patient_key, []).append((event_key, note, cleaned))

    documents: list[GraphRagDocument] = []
    for patient_key in sorted(grouped):
        entries = grouped[patient_key]
        sections = [
            "# 비식별 환자 임상 근거",
            "",
            "이 문서는 임상시험 기준 검토를 위한 비식별 진료 근거다.",
        ]
        for sequence, (event_key, note, cleaned) in enumerate(entries, start=1):
            note_type = mask_phi(str(note.get("note_type") or "진료 기록"))
            specialty = mask_phi(str(note.get("specialty") or "미지정"))
            sections.extend(
                [
                    "",
                    f"## 임상 이벤트 {sequence}",
                    f"- 이벤트 참조: {event_key}",
                    f"- 기록 유형: {note_type}",
                    f"- 진료 분야: {specialty}",
                    "",
                    cleaned,
                ]
            )

        metadata = {
            "metadataAttributes": {
                "patient_key": _metadata_attribute(
                    patient_key, include_for_embedding=False
                ),
                "document_type": _metadata_attribute(
                    "patient_evidence", include_for_embedding=True
                ),
                "schema_version": _metadata_attribute(
                    "graphrag-patient-v1", include_for_embedding=False
                ),
                "phi_status": _metadata_attribute(
                    "pseudonymized_regex_v1", include_for_embedding=False
                ),
            }
        }
        documents.append(
            GraphRagDocument(
                patient_key=patient_key,
                body="\n".join(sections).strip() + "\n",
                metadata=metadata,
                note_count=len(entries),
            )
        )
    return documents
