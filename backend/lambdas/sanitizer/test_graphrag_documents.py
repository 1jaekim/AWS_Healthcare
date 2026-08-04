from backend.lambdas.sanitizer.graphrag_documents import (
    build_patient_documents,
    pseudonymize_identifier,
)


SECRET = "unit-test-secret-that-is-not-used-in-production"


def test_pseudonymization_is_stable_and_namespaced() -> None:
    first = pseudonymize_identifier("pt", "123", SECRET)
    second = pseudonymize_identifier("pt", "123", SECRET)

    assert first == second
    assert first.startswith("pt_")
    assert "123" not in first
    assert pseudonymize_identifier("evt", "123", SECRET) != first


def test_builds_one_supported_document_per_patient() -> None:
    notes = [
        {
            "note_id": "NOTE-1-AUGMENTED",
            "person_id": 3,
            "encounter_id": "ENC-1",
            "canonical_flag": 0,
            "free_text_emr": "이 변형 노트는 GraphRAG에 들어가면 안 된다.",
        },
        {
            "note_id": "NOTE-1",
            "person_id": 3,
            "encounter_id": "ENC-1",
            "canonical_flag": 1,
            "note_type": "내분비 외래 SOAP",
            "specialty": "내분비내과",
            "free_text_emr": (
                "환자명: 홍길동. 2026-01-03 HbA1c 7.2%로 확인했다. "
                "임상시험 판정: SYN-T2D-01:ELIGIBLE"
            ),
        },
        {
            "note_id": "NOTE-2",
            "person_id": 4,
            "encounter_id": "ENC-2",
            "canonical_flag": 1,
            "free_text_emr": "공복혈당 130 mg/dL로 추적 관찰했다.",
        },
    ]

    documents = build_patient_documents(notes, pseudonymization_secret=SECRET)

    assert len(documents) == 2
    first = next(document for document in documents if "HbA1c" in document.body)
    assert first.note_count == 1
    assert "홍길동" not in first.body
    assert "2026-01-03" not in first.body
    assert "임상시험 판정" not in first.body
    assert "HbA1c 7.2%" in first.body
    assert "ENC-1" not in first.body
    assert "evt_" in first.body
    attrs = first.metadata["metadataAttributes"]
    assert attrs["patient_key"]["value"]["stringValue"] == first.patient_key
    assert attrs["patient_key"]["includeForEmbedding"] is False
    assert attrs["document_type"]["value"]["stringValue"] == "patient_evidence"
    assert (
        attrs["phi_status"]["value"]["stringValue"]
        == "pseudonymized_regex_v1"
    )


def test_accepts_already_deidentified_json_contract() -> None:
    documents = build_patient_documents(
        [
            {
                "patient_key": "pt_existing",
                "event_key": "evt_existing",
                "text": "비식별화가 완료된 환자 임상 기록입니다.",
            }
        ],
        pseudonymization_secret=SECRET,
    )

    assert documents[0].patient_key == "pt_existing"
    assert "evt_existing" in documents[0].body
