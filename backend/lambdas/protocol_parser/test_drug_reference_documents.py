from __future__ import annotations

import pytest

from backend.lambdas.protocol_parser.drug_reference_documents import (
    build_dur_coadministration_document,
    is_diabetes_dur_row,
    normalize_dur_row,
)


def _row(index: int) -> dict[str, str]:
    return {
        "DUR_SEQ": f"DUR-{index:03d}",
        "TYPE_NAME": "병용금기",
        "MIX_TYPE": "단일",
        "INGR_CODE": f"CODE-{index:03d}",
        "INGR_KOR_NAME": f"기준성분{index}",
        "INGR_ENG_NAME": f"Ingredient {index}",
        "CLASS": "[00001]기준분류",
        "MIXTURE_MIX_TYPE": "단일",
        "MIXTURE_INGR_CODE": f"PAIR-{index:03d}",
        "MIXTURE_INGR_KOR_NAME": f"관계성분{index}",
        "MIXTURE_INGR_ENG_NAME": f"Related ingredient {index}",
        "MIXTURE_CLASS": "[00002]관계분류",
        "NOTIFICATION_DATE": "20260101",
        "PROHBT_CONTENT": f"병용금기 사유 {index}",
        "DEL_YN": "정상",
    }


def test_normalizes_documented_uppercase_fields() -> None:
    normalized = normalize_dur_row(_row(1))
    assert normalized["ingredient_name_ko"] == "기준성분1"
    assert normalized["related_ingredient"] == "관계성분1"
    assert normalized["reason"] == "병용금기 사유 1"


def test_builds_ten_public_standard_relations_without_patient_key() -> None:
    document = build_dur_coadministration_document(
        [_row(index) for index in range(1, 11)],
        retrieved_at="2026-08-06T00:00:00+00:00",
    )
    attrs = document.metadata["metadataAttributes"]

    assert document.key.startswith("rag/references/standards/drugs/")
    assert attrs["document_type"]["value"]["stringValue"] == "standard_document"
    assert attrs["authority"]["value"]["stringValue"] == "MFDS"
    assert document.body.count("## ") == 10
    assert "기준성분1" in document.body
    assert "관계성분10" in document.body
    assert "patient_key" not in document.body
    assert "person_id" not in document.body


def test_rejects_more_than_ten_rows() -> None:
    with pytest.raises(ValueError, match="at most 10"):
        build_dur_coadministration_document([_row(index) for index in range(11)])


def test_rejects_rows_with_missing_relation_fields() -> None:
    row = _row(1)
    row["MIXTURE_INGR_KOR_NAME"] = ""
    with pytest.raises(ValueError, match="required fields"):
        build_dur_coadministration_document([row])


def test_diabetes_scope_requires_diabetes_drug_class() -> None:
    row = _row(1)
    assert not is_diabetes_dur_row(row)
    with pytest.raises(ValueError, match="당뇨병용제"):
        build_dur_coadministration_document([row], scope="diabetes")


def test_builds_separate_diabetes_document() -> None:
    row = _row(1)
    row["CLASS"] = "[03960]당뇨병용제"
    assert is_diabetes_dur_row(row)
    document = build_dur_coadministration_document([row], scope="diabetes")
    attrs = document.metadata["metadataAttributes"]
    assert document.key.endswith("mfds-dur-coadministration-diabetes-10.md")
    assert attrs["clinical_topic"]["value"]["stringValue"] == "diabetes"
