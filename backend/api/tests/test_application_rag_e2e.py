"""완성 지원서와 공개 GraphRAG 근거를 결합하는 API E2E 계약."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.auth import Principal, current_principal
from app.config import GraphRagSettings, settings
from app.container import build_container
from app.main import app, get_container


TRIAL_ID = "SYN-T2D-INTENSIFY-01"
OWNER_SUB = "application-e2e-user"


class PublicReferenceRag:
    """Bedrock Retrieve와 같은 응답을 돌려주는 공개문서 전용 테스트 대역."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "retrievalResults": [
                {
                    "content": {
                        "text": (
                            "공개 모집 공고 기준: 조절되지 않는 고혈압과 현재 임신은 "
                            "제외 기준이며 지원서 기재값으로 확인한다."
                        )
                    },
                    "score": 0.93,
                    "metadata": {
                        "document_type": "trial_notice",
                        "trial_id": TRIAL_ID,
                        "source_id": f"trial-notice:{TRIAL_ID}",
                        "title": "제2형 당뇨 임상시험 공개 모집 공고",
                    },
                    "location": {
                        "s3Location": {
                            "uri": (
                                "s3://public-reference-bucket/rag/references/trials/"
                                f"{TRIAL_ID}.md"
                            )
                        }
                    },
                }
            ]
        }


def _schema(application_id: str) -> dict:
    properties = {
        "age": {"type": "integer", "x-criterion-field": "age", "x-unit": "years"},
        "t2d_duration_days": {
            "type": "integer",
            "x-criterion-field": "t2d_duration_days",
            "x-unit": "days",
        },
        "hba1c": {"type": "number", "x-criterion-field": "hba1c", "x-unit": "%"},
        "hba1c_count_365d": {
            "type": "integer",
            "x-criterion-field": "hba1c_count_365d",
            "x-unit": "count",
        },
        "egfr": {
            "type": "number",
            "x-criterion-field": "egfr",
            "x-unit": "mL/min/1.73m2",
        },
        "stable_regimen_days": {
            "type": "integer",
            "x-criterion-field": "stable_regimen_days",
            "x-unit": "days",
        },
        "uncontrolled_bp": {
            "type": "boolean",
            "x-criterion-field": "uncontrolled_bp",
            "x-unit": "boolean",
        },
        "active_pregnancy": {
            "type": "boolean",
            "x-criterion-field": "active_pregnancy",
            "x-unit": "boolean",
        },
    }
    return {
        "schema_id": f"SCHEMA-{application_id}",
        "trial_id": TRIAL_ID,
        "json_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
        },
    }


def _application(application_id: str, hba1c) -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "application_id": application_id,
        "schema_id": f"SCHEMA-{application_id}",
        "trial_id": TRIAL_ID,
        "owner_sub": OWNER_SUB,
        "status": "COMPLETE",
        "created_at": now,
        "updated_at": now,
        "data": {
            "age": 54,
            "t2d_duration_days": 900,
            "hba1c": hba1c,
            "hba1c_count_365d": 3,
            "egfr": 72,
            "stable_regimen_days": 120,
            "uncontrolled_bp": False,
            "active_pregnancy": False,
        },
    }


@pytest.mark.parametrize(
    ("application_id", "hba1c", "expected"),
    [
        ("APP-E2E-OK", 8.2, "OK"),
        ("APP-E2E-NOT-OK", 12.0, "NOT_OK"),
        # 사용자가 수치를 모른다고 명시한 완성 JSON은 값을 추측하지 않고 UNKNOWN이다.
        ("APP-E2E-UNKNOWN", "모름", "UNKNOWN"),
    ],
)
def test_application_json_to_public_rag_to_explained_decision(
    application_id: str, hba1c, expected: str
) -> None:
    rag = PublicReferenceRag()
    container = build_container(
        settings.data_dir,
        graphrag_config=GraphRagSettings(
            knowledge_base_id="ABCDEFGHIJ",
            knowledge_base_region="us-east-1",
        ),
        retrieval_client=rag,
    )
    container.intake_store.save_schema(_schema(application_id))
    container.intake_store.save_application(_application(application_id, hba1c))

    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[current_principal] = lambda: Principal(
        subject=OWNER_SUB,
        person_id=None,
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                f"/api/v1/applications/{application_id}/screening",
                json={"actor": "application-e2e"},
            )
    finally:
        app.dependency_overrides.pop(get_container, None)
        app.dependency_overrides.pop(current_principal, None)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["person_id"] is None
    assert body["application_id"] == application_id
    assert body["screening_decision"] == expected
    assert body["packet"]["person_id"] is None
    assert all(item["explanation"] for item in body["packet"]["items"])

    assert len(rag.calls) == 2
    serialized_calls = json.dumps(rag.calls, ensure_ascii=False)
    assert "patient_key" not in serialized_calls
    assert "person_id" not in serialized_calls
    assert "trial_notice" in serialized_calls
    assert "standard_document" in serialized_calls

    narrative_items = [
        item for item in body["packet"]["items"] if item["narrative"]
    ]
    assert {item["field"] for item in narrative_items} == {
        "uncontrolled_bp",
        "active_pregnancy",
    }
    assert all(
        f"trial-notice:{TRIAL_ID}" in item["source_ids"]
        for item in narrative_items
    )
    assert all(
        any(source_id.startswith(f"{application_id}:") for source_id in item["source_ids"])
        for item in body["packet"]["items"]
    )
