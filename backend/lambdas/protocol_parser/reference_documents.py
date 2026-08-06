"""공개 임상시험 공고를 Bedrock Knowledge Base 문서로 변환한다."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class ReferenceDocument:
    key: str
    body: str
    metadata: dict[str, Any]


def _attribute(value: str, *, embed: bool) -> dict[str, Any]:
    return {
        "value": {"type": "STRING", "stringValue": value},
        "includeForEmbedding": embed,
    }


def _criteria_lines(title: str, items: list[dict[str, Any]]) -> list[str]:
    lines = [f"## {title}"]
    if not items:
        return [*lines, "- 공고에 명시된 기준 없음"]
    for item in items:
        criterion_id = str(item.get("id") or "기준")
        description = str(item.get("description") or "").strip()
        structured = item.get("structured")
        suffix = ""
        if isinstance(structured, dict):
            suffix = " 구조화 조건: " + json.dumps(
                structured, ensure_ascii=False, sort_keys=True
            )
        lines.append(f"- [{criterion_id}] {description}{suffix}".strip())
    return lines


def build_trial_notice_document(
    *, trial_data: dict[str, Any], source_key: str, source_text: str
) -> ReferenceDocument:
    """파싱된 기준과 공개 원문을 공고별 Markdown 및 sidecar로 만든다."""
    trial_id = str(trial_data.get("trial_id") or "UNKNOWN").strip()
    if not trial_id:
        raise ValueError("trial_id is required")
    title = str(trial_data.get("trial_title") or trial_id).strip()
    safe_id = _SAFE_ID.sub("-", trial_id).strip("-")
    if not safe_id:
        safe_id = hashlib.sha256(trial_id.encode("utf-8")).hexdigest()[:16]
    source_id = f"trial-notice:{trial_id}"
    sections = [
        f"# {title}",
        "",
        f"- 공고 ID: {trial_id}",
        f"- 대상 질환: {trial_data.get('condition') or '미기재'}",
        f"- 시험 단계: {trial_data.get('phase') or '미기재'}",
        f"- 중재: {trial_data.get('intervention') or '미기재'}",
        f"- 원본 객체: {source_key}",
        "",
        *_criteria_lines(
            "선정 기준", list(trial_data.get("inclusion_criteria") or [])
        ),
        "",
        *_criteria_lines(
            "제외 기준", list(trial_data.get("exclusion_criteria") or [])
        ),
        "",
        "## 공개 공고 원문",
        source_text.strip()[:50_000],
    ]
    metadata = {
        "metadataAttributes": {
            "document_type": _attribute("trial_notice", embed=True),
            "trial_id": _attribute(trial_id, embed=True),
            "source_id": _attribute(source_id, embed=False),
            "title": _attribute(title, embed=True),
            "schema_version": _attribute("graphrag-reference-v1", embed=False),
            "data_classification": _attribute("public", embed=False),
        }
    }
    return ReferenceDocument(
        key=f"rag/references/trials/{safe_id}.md",
        body="\n".join(sections).strip() + "\n",
        metadata=metadata,
    )


__all__ = ["ReferenceDocument", "build_trial_notice_document"]
