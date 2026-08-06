"""식약처 DUR 공개 성분정보를 GraphRAG 표준문서로 변환한다.

지원자 복약정보는 이 문서에 섞지 않는다. 이 모듈이 만드는 것은 식약처가
공개한 약물 관계와 출처뿐이며, 지원서 JSON은 판정기에 별도로 전달된다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .reference_documents import ReferenceDocument, _attribute


FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "dur_sequence": ("DUR_SEQ", "durSeq"),
    "dur_type": ("TYPE_NAME", "typeName"),
    "ingredient_mix_type": ("MIX_TYPE", "mixType"),
    "ingredient_code": ("INGR_CODE", "ingrCode", "DUR_CODE", "durCode"),
    "ingredient_name_ko": ("INGR_KOR_NAME", "ingrKorName"),
    "ingredient_name_en": ("INGR_ENG_NAME", "ingrEngName"),
    "ingredient_class": ("CLASS", "className"),
    "related_mix_type": ("MIXTURE_MIX_TYPE", "mixtureMixType"),
    "related_ingredient_code": ("MIXTURE_INGR_CODE", "mixtureIngrCode"),
    "related_ingredient": (
        "MIXTURE_INGR_KOR_NAME",
        "mixtureIngrKorName",
        "RELATION_INGR",
        "relationIngr",
    ),
    "related_ingredient_en": (
        "MIXTURE_INGR_ENG_NAME",
        "mixtureIngrEngName",
    ),
    "related_ingredient_class": ("MIXTURE_CLASS", "mixtureClass"),
    "notification_date": ("NOTIFICATION_DATE", "notificationDate"),
    "reason": ("PROHBT_CONTENT", "prohbtContent"),
    "status": ("DEL_YN", "delYn"),
}


def _value(row: dict[str, Any], name: str) -> str:
    for key in FIELD_ALIASES[name]:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def normalize_dur_row(row: dict[str, Any]) -> dict[str, str]:
    """API의 대문자/카멜표기 변형을 하나의 내부 계약으로 맞춘다."""
    return {name: _value(row, name) for name in FIELD_ALIASES}


def is_diabetes_dur_row(row: dict[str, Any]) -> bool:
    """관계의 어느 한쪽이라도 식약처 분류가 당뇨병용제인지 확인한다."""
    normalized = normalize_dur_row(row)
    return any(
        "당뇨병용제" in normalized[field]
        for field in ("ingredient_class", "related_ingredient_class")
    )


def build_dur_coadministration_document(
    rows: list[dict[str, Any]],
    *,
    retrieved_at: str | None = None,
    scope: str = "pilot",
) -> ReferenceDocument:
    """병용금기 API 응답 최대 10건을 공개 표준문서 한 건으로 만든다."""
    if not rows:
        raise ValueError("DUR rows are required")
    if len(rows) > 10:
        raise ValueError("DUR pilot document accepts at most 10 rows")
    if scope not in {"pilot", "diabetes"}:
        raise ValueError("scope must be 'pilot' or 'diabetes'")
    if scope == "diabetes" and any(not is_diabetes_dur_row(row) for row in rows):
        raise ValueError("diabetes scope accepts only 당뇨병용제 relations")

    normalized = [normalize_dur_row(row) for row in rows]
    required = (
        "ingredient_code",
        "ingredient_name_ko",
        "related_ingredient_code",
        "related_ingredient",
        "reason",
    )
    invalid = [
        index
        for index, row in enumerate(normalized, start=1)
        if any(not row[field] for field in required)
    ]
    if invalid:
        raise ValueError(f"DUR required fields are missing in rows: {invalid}")
    timestamp = retrieved_at or datetime.now(UTC).isoformat()
    scope_label = "당뇨병용제 병용금기 10건" if scope == "diabetes" else "병용금기 파일럿 10건"
    lines = [
        f"# 식품의약품안전처 DUR 성분정보 — {scope_label}",
        "",
        "- 제공기관: 식품의약품안전처",
        "- 관계 유형: 병용금기",
        f"- API 조회시각(UTC): {timestamp}",
        "- 사용 목적: 임상시험 공고의 명시적 약물 제외조건을 해석하는 공개 근거",
        "- 주의: 이 문서만으로 임상시험 부적격을 확정하지 않으며 공고 원문과 함께 확인",
        "",
    ]
    for index, item in enumerate(normalized, start=1):
        source_id = item["dur_sequence"] or (
            f"{item['ingredient_code']}-{item['related_ingredient_code']}"
        )
        lines.extend(
            [
                f"## {index}. DUR {source_id}",
                f"- DUR 유형: {item['dur_type'] or '병용금기'}",
                f"- 기준 성분: {item['ingredient_name_ko'] or '미기재'}",
                f"- 기준 성분 영문명: {item['ingredient_name_en'] or '미기재'}",
                f"- DUR 성분코드: {item['ingredient_code'] or '미기재'}",
                f"- 기준 성분 분류: {item['ingredient_class'] or '미기재'}",
                f"- 기준 단일/복합: {item['ingredient_mix_type'] or '미기재'}",
                f"- 관계 성분: {item['related_ingredient'] or '미기재'}",
                f"- 관계 성분 영문명: {item['related_ingredient_en'] or '미기재'}",
                f"- 관계 성분코드: {item['related_ingredient_code'] or '미기재'}",
                f"- 관계 성분 분류: {item['related_ingredient_class'] or '미기재'}",
                f"- 관계 단일/복합: {item['related_mix_type'] or '미기재'}",
                f"- 고시일자: {item['notification_date'] or '미기재'}",
                f"- 금기내용: {item['reason'] or '미기재'}",
                f"- 데이터 상태: {item['status'] or '미기재'}",
                "",
            ]
        )

    source_id = f"standard:mfds-dur:coadministration:{scope}-10"
    title = f"식품의약품안전처 DUR 성분정보 {scope_label}"
    metadata = {
        "metadataAttributes": {
            "document_type": _attribute("standard_document", embed=True),
            "source_id": _attribute(source_id, embed=False),
            "title": _attribute(title, embed=True),
            "schema_version": _attribute("graphrag-drug-standard-v1", embed=False),
            "data_classification": _attribute("public", embed=False),
            "authority": _attribute("MFDS", embed=True),
            "relation_type": _attribute(
                "coadministration_contraindication", embed=True
            ),
            "clinical_topic": _attribute(
                "diabetes" if scope == "diabetes" else "general", embed=True
            ),
        }
    }
    return ReferenceDocument(
        key=(
            "rag/references/standards/drugs/"
            f"mfds-dur-coadministration-{scope}-10.md"
        ),
        body="\n".join(lines).rstrip() + "\n",
        metadata=metadata,
    )


__all__ = [
    "build_dur_coadministration_document",
    "is_diabetes_dur_row",
    "normalize_dur_row",
]
