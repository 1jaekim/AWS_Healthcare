"""공고 기반 반복형 자연어 지원서 수집 검증."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from agent.model import ModelResponse
from app.auth import Principal
from app.intake.service import (
    BASE_PROPERTIES,
    FollowUpLimitReached,
    IntakeService,
    InvalidGeneratedSchema,
)
from app.intake.store import IntakeStore
from app.intake.model import StubApplicationModelClient
from app.intake.screening import ApplicationSupplementBuilder
from app.schemas import ApplicationScreeningRequest


class ScriptedIntakeModel:
    mode = "scripted"

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._payloads = list(payloads)

    def converse(self, *, conversation, system, tools=None):
        return ModelResponse(
            text=json.dumps(self._payloads.pop(0), ensure_ascii=False)
        )


def _service(*payloads: dict[str, Any]) -> IntakeService:
    return IntakeService(
        store=IntakeStore(), model=ScriptedIntakeModel(list(payloads))
    )


def test_schema_keeps_base_fields_and_adds_notice_fields() -> None:
    service = _service()
    result = service.generate_schema(
        trial_id="TRIAL-1",
        notice_text="HbA1c 7.5 이상인 지원자를 모집합니다.",
        additional_fields=[
            {
                "name": "latest_hba1c",
                "type": "number",
                "title": "최근 HbA1c",
                "description": "가장 최근 HbA1c 검사 결과",
            }
        ],
    )

    properties = result["json_schema"]["properties"]
    assert set(BASE_PROPERTIES).issubset(properties)
    assert properties["age"]["x-source"] == "base"
    assert properties["latest_hba1c"]["x-source"] == "trial_notice"
    assert set(result["json_schema"]["required"]) == {
        *BASE_PROPERTIES,
        "latest_hba1c",
    }


def test_completed_json_maps_scalar_fields_to_orchestrator_supplements() -> None:
    application = {
        "application_id": "APP-1",
        "updated_at": "2026-08-04T10:00:00+00:00",
        "data": {
            "age": 42,
            "latest_hba1c": 8.1,
            "diagnosed_conditions": ["제2형 당뇨병"],
        },
    }
    schema = {
        "properties": {
            "age": {"type": "integer", "title": "만 나이"},
            "latest_hba1c": {
                "type": "number",
                "title": "최근 HbA1c",
                "x-criterion-field": "hba1c",
                "x-unit": "%",
            },
            "diagnosed_conditions": {
                "type": "array",
                "title": "현재 진단 질환",
            },
        }
    }

    result = ApplicationSupplementBuilder().build(
        application=application, json_schema=schema
    )

    assert set(result.observations) == {"age", "hba1c"}
    assert result.observations["hba1c"].value == 8.1
    assert result.observations["hba1c"].unit == "%"
    assert result.observations["hba1c"].source == "PATIENT_REPORTED"
    assert result.observations["hba1c"].source_id == "APP-1:latest_hba1c"
    assert result.skipped == (
        {
            "field": "diagnosed_conditions",
            "reason": "목록 값은 단일 기준 관찰값으로 승격하지 않습니다.",
        },
    )


def test_completed_application_calls_orchestrator_with_json_supplements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main

    application = {
        "application_id": "APP-1",
        "trial_id": "TRIAL-1",
        "status": "COMPLETE",
        "updated_at": "2026-08-04T10:00:00+00:00",
        "data": {"latest_hba1c": 8.1},
    }
    schema = {
        "json_schema": {
            "properties": {
                "latest_hba1c": {
                    "type": "number",
                    "title": "최근 HbA1c",
                    "x-criterion-field": "hba1c",
                    "x-unit": "%",
                }
            }
        }
    }

    class Orchestrator:
        call: dict[str, Any] | None = None

        def run(self, **kwargs: Any) -> SimpleNamespace:
            self.call = kwargs
            return SimpleNamespace(
                run=SimpleNamespace(
                    run_id="RUN-1",
                    metadata={"supplements": {"applied": ["hba1c"], "ignored": []}}
                )
            )

    class Audit:
        events: list[tuple[str, dict[str, Any]]] = []

        def record(self, event: str, **payload: Any) -> None:
            self.events.append((event, payload))

    orchestrator = Orchestrator()
    container = SimpleNamespace(
        repository=SimpleNamespace(patients={7: {}}, trials={"TRIAL-1": {}}),
        application_intake=SimpleNamespace(
            completed_application=lambda application_id, **kwargs: (application, schema)
        ),
        orchestrator=orchestrator,
        audit=Audit(),
        retrieval_mode="bedrock_graphrag",
    )
    monkeypatch.setattr(main, "_run_response", lambda container, output: {})

    response = main.screen_completed_application(
        "APP-1",
        ApplicationScreeningRequest(person_id=7, actor="tester"),
        container,
        # 엔드포인트를 직접 부르므로 의존성이 주입되지 않는다. 본인 환자 접근
        # 검사를 통과하는 주체를 넘긴다.
        Principal(subject="tester", person_id=7),
    )

    assert orchestrator.call is not None
    assert orchestrator.call["person_id"] == 7
    assert orchestrator.call["trial_id"] == "TRIAL-1"
    assert orchestrator.call["supplements"]["hba1c"].value == 8.1
    assert response["supplements"]["source_application_id"] == "APP-1"
    assert response["supplements"]["retrieval_mode"] == "bedrock_graphrag"
    assert container.audit.events[0][0] == "APPLICATION_SCREENING_LINKED"


def test_notice_cannot_override_a_base_field() -> None:
    service = _service()
    with pytest.raises(InvalidGeneratedSchema):
        service.generate_schema(
            trial_id="TRIAL-1",
            notice_text="나이 제한",
            additional_fields=[
                {
                    "name": "age",
                    "type": "string",
                    "title": "나이",
                    "description": "잘못된 덮어쓰기",
                }
            ],
        )


def test_stub_requires_explicit_notice_fields() -> None:
    service = IntakeService(
        store=IntakeStore(), model=StubApplicationModelClient()
    )
    with pytest.raises(InvalidGeneratedSchema, match="additional_fields"):
        service.generate_schema(trial_id="TRIAL-1", notice_text="HbA1c 기준")


def test_application_repeats_until_every_required_field_is_answered() -> None:
    service = _service(
        {
            "values": {
                "age": 42,
                "sex": "female",
                "diagnosed_conditions": ["제2형 당뇨병"],
            }
        },
        {
            "values": {
                "current_medications": ["메트포르민"],
                "allergies": [],
                "prior_trial_participation": False,
                "latest_hba1c": 8.1,
            }
        },
    )
    schema = service.generate_schema(
        trial_id="TRIAL-1",
        notice_text="최근 HbA1c 수치가 필요합니다.",
        additional_fields=[
            {
                "name": "latest_hba1c",
                "type": "number",
                "title": "최근 HbA1c",
                "description": "최근 검사 수치",
            }
        ],
    )

    first = service.start_application(
        schema_id=schema["schema_id"], application_text="저는 42세 여성입니다."
    )
    assert first["status"] == "NEEDS_MORE_INFO"
    assert first["follow_up_count"] == 1
    assert first["notice_text"] == "최근 HbA1c 수치가 필요합니다."
    assert {item["name"] for item in first["missing_fields"]} == {
        "current_medications",
        "allergies",
        "prior_trial_participation",
        "latest_hba1c",
    }

    completed = service.add_response(
        first["application_id"], "나머지 정보를 추가합니다."
    )
    assert completed["status"] == "COMPLETE"
    assert completed["missing_fields"] == []
    assert completed["follow_up_prompt"] is None
    assert completed["data"]["allergies"] == []
    assert completed["data"]["prior_trial_participation"] is False
    assert completed["data"]["latest_hba1c"] == 8.1
    assert completed["iteration"] == 2


def test_array_answers_accumulate_deduplicate_and_can_be_cleared() -> None:
    service = _service(
        {
            "values": {
                "age": 40,
                "sex": "male",
                "diagnosed_conditions": ["당뇨병"],
                "current_medications": ["메트포르민"],
                "allergies": ["페니실린"],
                "prior_trial_participation": False,
            }
        },
        {
            "values": {
                "diagnosed_conditions": ["당뇨병", "고혈압"],
                "current_medications": ["인슐린"],
            }
        },
        {"values": {"allergies": "없음"}},
    )
    schema = service.generate_schema(
        trial_id="TRIAL-ARRAY",
        notice_text="기본 정보 확인",
        additional_fields=[
            {
                "name": "final_confirmation",
                "type": "string",
                "title": "최종 확인",
                "description": "배열 정정 테스트 동안 세션을 열어두는 필드",
            }
        ],
    )
    first = service.start_application(
        schema_id=schema["schema_id"], application_text="최초 지원서"
    )
    accumulated = service.add_response(
        first["application_id"], "고혈압과 인슐린도 추가합니다."
    )
    assert accumulated["data"]["diagnosed_conditions"] == ["당뇨병", "고혈압"]
    assert accumulated["data"]["current_medications"] == ["메트포르민", "인슐린"]

    cleared = service.add_response(first["application_id"], "알레르기는 없습니다.")
    assert cleared["data"]["allergies"] == []


def test_follow_up_questions_stop_after_five() -> None:
    service = _service(*[{"values": {}} for _ in range(6)])
    schema = service.generate_schema(
        trial_id="TRIAL-LIMIT",
        notice_text="기본 정보 확인",
        additional_fields=[],
    )
    state = service.start_application(
        schema_id=schema["schema_id"], application_text="정보를 적지 않았습니다."
    )
    assert state["follow_up_count"] == 1

    for number in range(2, 6):
        state = service.add_response(state["application_id"], "잘 모르겠습니다.")
        assert state["status"] == "NEEDS_MORE_INFO"
        assert state["follow_up_count"] == number

    state = service.add_response(state["application_id"], "여전히 답변이 없습니다.")
    assert state["status"] == "MAX_FOLLOW_UPS_REACHED"
    assert state["follow_up_count"] == 5
    assert state["follow_up_prompt"] is None
    assert state["missing_fields"]

    with pytest.raises(FollowUpLimitReached):
        service.add_response(state["application_id"], "여섯 번째 추가 답변")
