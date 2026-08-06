from backend.lambdas.protocol_parser.reference_documents import (
    build_trial_notice_document,
)


def test_builds_public_trial_notice_with_reference_metadata() -> None:
    document = build_trial_notice_document(
        trial_data={
            "trial_id": "NCT-TEST-1",
            "trial_title": "당뇨 임상시험",
            "condition": "제2형 당뇨병",
            "inclusion_criteria": [
                {
                    "id": "INC-1",
                    "description": "HbA1c 7.5% 이상",
                    "structured": {
                        "parameter": "hba1c",
                        "operator": ">=",
                        "value": 7.5,
                        "unit": "%",
                    },
                }
            ],
            "exclusion_criteria": [],
        },
        source_key="trials/NCT-TEST-1.pdf",
        source_text="공개 모집 공고 원문",
    )

    attrs = document.metadata["metadataAttributes"]
    assert document.key == "rag/references/trials/NCT-TEST-1.md"
    assert attrs["document_type"]["value"]["stringValue"] == "trial_notice"
    assert attrs["trial_id"]["value"]["stringValue"] == "NCT-TEST-1"
    assert attrs["data_classification"]["value"]["stringValue"] == "public"
    assert "patient_key" not in attrs
    assert "HbA1c 7.5% 이상" in document.body
