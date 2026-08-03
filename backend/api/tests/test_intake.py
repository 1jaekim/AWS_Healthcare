"""Intake 에이전트 검증.

자유 문장을 이벤트로 정규화하는 경로를 확인한다. 모델 호출은 가짜 클라이언트로
주입하고, 규칙 추출기만 쓰는 결정론적 경로도 함께 본다.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.intake import (
    IntakeAgent,
    IntakeResult,
    RuleIntakeExtractor,
    parse_when,
)
from agent.model import ModelError, ModelResponse, StubModelClient
from app.config import settings
from app.container import build_container
from app.domain.intake_vocabulary import CatalogFieldResolver
from app.main import app
from app.safety.guardrails import LocalGuardrail
from app.safety.observability import TraceCollector

REFERENCE = "2024-06-15"


# ---------------------------------------------------------------------------
# 테스트 더블
# ---------------------------------------------------------------------------


class ScriptedModel:
    """정해진 JSON 을 돌려주는 모델."""

    mode = "scripted"

    def __init__(self, payload: dict[str, Any] | str) -> None:
        self._text = (
            payload
            if isinstance(payload, str)
            else json.dumps(payload, ensure_ascii=False)
        )
        self.calls = 0

    def converse(self, *, conversation, system, tools=None):
        self.calls += 1
        return ModelResponse(text=self._text, input_tokens=11, output_tokens=7)


class FailingModel:
    mode = "failing"

    def converse(self, *, conversation, system, tools=None):
        raise ModelError("의도적 실패")


def make_agent(model=None, *, resolver=True) -> IntakeAgent:
    return IntakeAgent(
        guardrail=LocalGuardrail(attach_disclaimer=False),
        trace=TraceCollector(),
        model=model,
        resolver=CatalogFieldResolver() if resolver else None,
    )


def normalize(text: str, model=None, *, reference: str = REFERENCE) -> IntakeResult:
    return make_agent(model).normalize(text, reference_date=reference)


def terms(result: IntakeResult) -> dict[str, Any]:
    return {item.term: item.value for item in result.events}


# ---------------------------------------------------------------------------
# 날짜 정규화
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected", "precision"),
    [
        ("오늘", "2024-06-15", "DAY"),
        ("어제", "2024-06-14", "DAY"),
        ("그제", "2024-06-13", "DAY"),
        ("10일 전", "2024-06-05", "DAY"),
        ("2주 전", "2024-06-01", "APPROX"),
        ("3개월 전", "2024-03-15", "APPROX"),
        ("2년 전", "2022-06-15", "APPROX"),
        ("지난주", "2024-06-08", "APPROX"),
        ("지난달", "2024-05-15", "MONTH"),
        ("작년", "2023-06-15", "APPROX"),
        ("2024년 5월 3일", "2024-05-03", "DAY"),
        ("2024년 5월", "2024-05-01", "MONTH"),
        ("2024-05-03", "2024-05-03", "DAY"),
        ("2024.5.3", "2024-05-03", "DAY"),
        ("올해 3월", "2024-03-01", "MONTH"),
        ("작년 12월", "2023-12-01", "MONTH"),
    ],
)
def test_parse_when_정규화(expression: str, expected: str, precision: str) -> None:
    iso, got_precision, note = parse_when(expression, date(2024, 6, 15))
    assert iso == expected
    assert got_precision == precision
    assert note is None


def test_parse_when_연도없는_날짜는_기준일_이전으로_본다() -> None:
    # 기준일(6/15) 이후인 8월 3일은 작년으로 해석한다.
    iso, precision, note = parse_when("8월 3일", date(2024, 6, 15))
    assert iso == "2023-08-03"
    assert precision == "DAY"
    assert note is None


def test_parse_when_월말은_해당월_마지막날로_자른다() -> None:
    # 3월 31일 기준 1개월 전은 2월 29일(윤년)이다.
    iso, _, _ = parse_when("1개월 전", date(2024, 3, 31))
    assert iso == "2024-02-29"


def test_parse_when_해석실패는_메모를_남긴다() -> None:
    iso, precision, note = parse_when("언젠가 예전에", date(2024, 6, 15))
    assert iso is None
    assert precision is None
    assert note is not None


def test_parse_when_빈값은_조용히_통과한다() -> None:
    assert parse_when(None, date(2024, 6, 15)) == (None, None, None)


# ---------------------------------------------------------------------------
# 규칙 추출
# ---------------------------------------------------------------------------


def test_측정값과_단위를_추출하고_기준필드로_정교화한다() -> None:
    result = normalize("3개월 전 HbA1c 7.8%, 공복혈당 142 mg/dL 였습니다.")

    hba1c = next(item for item in result.events if item.term == "hba1c")
    assert hba1c.event_type == "MEASUREMENT"
    assert hba1c.value == 7.8
    assert hba1c.unit == "%"
    assert hba1c.field_name == "hba1c"
    assert hba1c.occurred_at == "2024-03-15"
    assert hba1c.date_precision == "APPROX"
    assert hba1c.origin == "rule"
    assert hba1c.needs_review is False

    glucose = next(item for item in result.events if item.term == "fasting_glucose")
    assert glucose.value == 142.0
    assert glucose.field_name == "fasting_glucose"


def test_단위가_없으면_카탈로그_단위를_채운다() -> None:
    result = normalize("어제 HbA1c 6.4 였습니다.")
    hba1c = next(item for item in result.events if item.term == "hba1c")
    assert hba1c.unit == "%"
    assert hba1c.needs_review is False


def test_혈압은_수축기와_이완기_두_이벤트로_나눈다() -> None:
    result = normalize("혈압 150/95 mmHg 측정했습니다.")
    values = terms(result)
    assert values["systolic_bp"] == 150.0
    assert values["diastolic_bp"] == 95.0


def test_카탈로그에_없는_필드는_기록만_남고_연결되지_않는다() -> None:
    result = normalize("체중 82.5 kg 입니다.")
    weight = next(item for item in result.events if item.term == "weight_kg")
    assert weight.value == 82.5
    assert weight.field_name is None
    assert any("등록된 기준 필드" in note for note in weight.notes)


def test_부정_표현은_값을_False로_둔다() -> None:
    result = normalize("저혈당은 없었습니다.")
    hypo = next(item for item in result.events if item.term == "hypoglycemia")
    assert hypo.event_type == "ADVERSE_EVENT"
    assert hypo.value is False
    assert hypo.unit == "boolean"


def test_증상_보고는_값을_True로_둔다() -> None:
    result = normalize("요즘 어지러움과 오심이 있습니다.")
    values = terms(result)
    assert values["dizziness"] is True
    assert values["nausea"] is True


def test_부정은_문장_단위로만_적용된다() -> None:
    result = normalize("저혈당은 없었습니다. 두통이 있습니다.")
    values = terms(result)
    assert values["hypoglycemia"] is False
    assert values["headache"] is True


def test_측정값은_같은_문장의_부정에_영향받지_않는다() -> None:
    # 숫자가 적힌 측정값은 사실이다. 문장에 '없음' 이 있어도 값을 뒤집지 않는다.
    result = normalize("HbA1c 7.1% 이며 이전 검사값은 없음.")
    hba1c = next(item for item in result.events if item.term == "hba1c")
    assert hba1c.value == 7.1


def test_약물_상태와_동작을_추출한다() -> None:
    result = normalize("지난달부터 metformin 을 중단했습니다.")
    med = next(item for item in result.events if item.term == "metformin")
    assert med.event_type == "MEDICATION"
    assert med.value == "STOPPED"
    assert med.occurred_at == "2024-05-15"


def test_약물_동작이_없으면_언급으로_남긴다() -> None:
    result = normalize("현재 인슐린 처방을 받고 있습니다.")
    med = next(item for item in result.events if item.term == "insulin")
    assert med.value == "MENTIONED"


def test_상태는_기준필드로_연결된다() -> None:
    result = normalize("임신 중입니다.")
    condition = next(
        item for item in result.events if item.term == "active_pregnancy"
    )
    assert condition.event_type == "CONDITION"
    assert condition.value is True
    assert condition.field_name == "active_pregnancy"


def test_빈_입력은_빈_결과를_낸다() -> None:
    result = normalize("   ")
    assert result.events == ()
    assert result.text == ""


def test_같은_입력은_같은_결과를_낸다() -> None:
    text = "3개월 전 HbA1c 7.8%, 혈압 150/95 mmHg. 저혈당은 없었습니다."
    first = normalize(text)
    second = normalize(text)
    assert [item.to_dict() for item in first.events] == [
        item.to_dict() for item in second.events
    ]


# ---------------------------------------------------------------------------
# 검증 규칙
# ---------------------------------------------------------------------------


def test_단위가_어긋나면_검토_대상으로_올린다() -> None:
    result = normalize("HbA1c 7.2 mg/dL 입니다.")
    hba1c = next(item for item in result.events if item.term == "hba1c")
    assert hba1c.needs_review is True
    assert any("단위 표기가 다릅니다" in note for note in hba1c.notes)


def test_통상_범위를_벗어난_값은_검토_대상으로_올린다() -> None:
    result = normalize("HbA1c 45.0% 입니다.")
    hba1c = next(item for item in result.events if item.term == "hba1c")
    assert hba1c.value == 45.0
    assert hba1c.needs_review is True
    assert any("통상 범위" in note for note in hba1c.notes)


def test_해석하지_못한_시점은_검토_대상으로_올린다() -> None:
    agent = make_agent(
        ScriptedModel(
            {
                "events": [
                    {
                        "type": "MEASUREMENT",
                        "term": "HbA1c",
                        "value": 7.0,
                        "unit": "%",
                        "when": "언젠가 예전에",
                        "span": "HbA1c 수치",
                        "confidence": 0.9,
                    }
                ]
            }
        )
    )
    result = agent.normalize("HbA1c 수치를 기억합니다.", reference_date=REFERENCE)
    event = next(item for item in result.events if item.origin == "model")
    assert event.occurred_at is None
    assert event.needs_review is True


def test_기준일보다_뒤의_시점은_검토_대상으로_올린다() -> None:
    result = normalize("내일 HbA1c 7.0% 예정입니다.", reference="2024-06-15")
    # '내일' 은 시점 표현으로 인식하지 않으므로 날짜 없이 통과한다.
    hba1c = next(item for item in result.events if item.term == "hba1c")
    assert hba1c.occurred_at is None

    future = normalize("2024년 9월 1일 HbA1c 7.0% 입니다.", reference="2024-06-15")
    event = next(item for item in future.events if item.term == "hba1c")
    assert event.occurred_at == "2024-09-01"
    assert event.needs_review is True
    assert any("기준일보다 뒤" in note for note in event.notes)


# ---------------------------------------------------------------------------
# 모델 경로
# ---------------------------------------------------------------------------


def test_원문에_없는_근거는_통과시키지_않는다() -> None:
    agent = make_agent(
        ScriptedModel(
            {
                "events": [
                    {
                        "type": "MEASUREMENT",
                        "term": "HbA1c",
                        "value": 9.9,
                        "unit": "%",
                        "when": "어제",
                        "span": "HbA1c 9.9%",
                        "confidence": 0.95,
                    }
                ]
            }
        )
    )
    result = agent.normalize("어제 병원에 다녀왔습니다.", reference_date=REFERENCE)
    assert result.events == ()
    assert len(result.dropped) == 1
    assert result.dropped[0]["reason"] == "근거 구간이 원문에 없습니다."


def test_모델이_규칙과_같은_사실을_내면_중복으로_제거한다() -> None:
    agent = make_agent(
        ScriptedModel(
            {
                "events": [
                    {
                        "type": "ADVERSE_EVENT",
                        "term": "케톤산증",
                        "value": True,
                        "when": None,
                        "span": "케톤산증 의심",
                        "confidence": 0.9,
                    }
                ]
            }
        )
    )
    result = agent.normalize("어제 케톤산증 의심 소견이 있었습니다.", reference_date=REFERENCE)
    assert [item.term for item in result.events] == ["ketoacidosis"]
    assert result.events[0].origin == "rule"
    assert any("중복" in item["reason"] for item in result.dropped)


def test_모델이_규칙이_놓친_사실을_보태면_받아들인다() -> None:
    agent = make_agent(
        ScriptedModel(
            {
                "events": [
                    {
                        "type": "ADVERSE_EVENT",
                        "term": "손발 저림",
                        "value": True,
                        "when": None,
                        "span": "손발 저림",
                        "confidence": 0.9,
                    }
                ]
            }
        )
    )
    result = agent.normalize("최근 손발 저림 증상이 있습니다.", reference_date=REFERENCE)
    event = next(item for item in result.events if item.origin == "model")
    assert event.term == "손발 저림"
    assert event.confidence <= 0.8, "모델 추출은 확신도 상한을 넘지 않는다"


def test_모델_호출이_실패해도_규칙_결과는_남는다() -> None:
    agent = make_agent(FailingModel())
    result = agent.normalize("HbA1c 7.8% 입니다.", reference_date=REFERENCE)
    assert any(item.term == "hba1c" for item in result.events)
    assert result.error is not None


def test_모델_응답_형식이_깨지면_규칙_결과만_쓴다() -> None:
    agent = make_agent(ScriptedModel("이건 JSON 이 아니다"))
    result = agent.normalize("HbA1c 7.8% 입니다.", reference_date=REFERENCE)
    assert any(item.term == "hba1c" for item in result.events)
    assert result.error is not None


def test_모델_출력의_형식_이상_항목은_조용히_뺀다() -> None:
    agent = make_agent(
        ScriptedModel(
            {
                "events": [
                    {"type": "UNKNOWN_TYPE", "term": "x", "span": "HbA1c"},
                    {"type": "MEASUREMENT", "span": "HbA1c"},
                    {"type": "MEASUREMENT", "term": "HbA1c"},
                    "문자열",
                ]
            }
        )
    )
    result = agent.normalize("HbA1c 7.8% 입니다.", reference_date=REFERENCE)
    assert [item.origin for item in result.events] == ["rule"]


def test_스텁_모델은_규칙_결과를_바꾸지_않는다() -> None:
    text = "3개월 전 HbA1c 7.8%, 혈압 150/95 mmHg. 저혈당은 없었습니다."
    rule_only = normalize(text)
    with_stub = normalize(text, StubModelClient())
    assert [item.to_dict() for item in rule_only.events] == [
        item.to_dict() for item in with_stub.events
    ]


def test_resolver가_없으면_필드_연결을_생략한다() -> None:
    agent = IntakeAgent(
        guardrail=LocalGuardrail(attach_disclaimer=False),
        trace=TraceCollector(),
        resolver=None,
    )
    result = agent.normalize("HbA1c 7.8% 입니다.", reference_date=REFERENCE)
    hba1c = next(item for item in result.events if item.term == "hba1c")
    assert hba1c.field_name is None
    assert hba1c.value == 7.8


def test_모델_호출은_trace_스팬을_남긴다() -> None:
    trace = TraceCollector()
    agent = IntakeAgent(
        guardrail=LocalGuardrail(attach_disclaimer=False),
        trace=trace,
        model=ScriptedModel({"events": []}),
        resolver=CatalogFieldResolver(),
    )
    agent.normalize("HbA1c 7.8%", reference_date=REFERENCE, run_id="RUN-1")
    names = [span["name"] for span in trace.spans_for("RUN-1")]
    assert "model:intake_normalizer" in names


def test_규칙_추출기는_단독으로_쓸_수_있다() -> None:
    found = RuleIntakeExtractor().extract("HbA1c 7.8% 입니다.")
    assert [item.term for item in found] == ["hba1c"]
    assert found[0].origin == "rule"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as instance:
        yield instance


def test_intake_normalize_엔드포인트(client: TestClient) -> None:
    response = client.post(
        "/api/v1/intake/normalize",
        json={
            "text": "3개월 전 HbA1c 7.8% 였고 혈압 150/95 mmHg 입니다.",
            "reference_date": REFERENCE,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reference_date"] == REFERENCE
    assert body["event_count"] == len(body["events"])
    found = {item["term"]: item for item in body["events"]}
    assert found["hba1c"]["value"] == 7.8
    assert found["hba1c"]["field"] == "hba1c"
    assert found["hba1c"]["occurred_at"] == "2024-03-15"
    assert found["systolic_bp"]["value"] == 150.0


def test_intake_normalize_빈_문장은_거부한다(client: TestClient) -> None:
    response = client.post("/api/v1/intake/normalize", json={"text": ""})
    assert response.status_code == 422


def test_답변_제출_응답에_정규화_결과가_붙는다(client: TestClient) -> None:
    patients = client.get("/api/v1/patients", params={"limit": 1}).json()
    person_id = patients["items"][0]["person_id"]
    trial_id = client.get("/api/v1/trials").json()[0]["trial_id"]

    run = client.post(
        "/api/v1/screening/run",
        json={"person_id": person_id, "trial_id": trial_id},
    ).json()
    criterion_id = run["packet"]["items"][0]["criterion_id"]

    response = client.post(
        f"/api/v1/patients/{person_id}/answers",
        json={
            "run_id": run["run_id"],
            "criterion_id": criterion_id,
            "value": "3개월 전 HbA1c 7.8% 였습니다. 저혈당은 없었습니다.",
        },
    )
    assert response.status_code == 200
    body = response.json()
    # 원문은 그대로 보관하고 해석을 덧붙인다.
    assert body["value"].startswith("3개월 전")
    intake = body["intake"]
    found = {item["term"]: item for item in intake["events"]}
    assert found["hba1c"]["value"] == 7.8
    assert found["hypoglycemia"]["value"] is False
    # 기준일은 실행의 인덱스 방문일이다.
    assert intake["reference_date"] == run["index_date"]


def test_아키텍처_응답에_intake_에이전트가_켜져있다(client: TestClient) -> None:
    body = client.get("/api/v1/architecture").json()
    agents = {item["name"]: item for item in body["agent"]["managed_agents"]}
    intake = agents["intake_agent"]
    assert intake["enabled"] is True
    assert intake["source"] == "agent/intake.py"


def test_컨테이너가_intake를_노출한다() -> None:
    container = build_container(settings.data_dir)
    result = container.intake.normalize("HbA1c 7.8%", reference_date=REFERENCE)
    assert any(item.term == "hba1c" for item in result.events)
