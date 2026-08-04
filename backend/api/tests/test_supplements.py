"""참여자 답변 승격과 재판정 검증.

Intake 가 정규화한 이벤트가 관찰값으로 올라가 미해소 기준을 움직이는지 본다.
핵심은 '움직이되 확정하지 않는다' 다. 답변은 UNKNOWN 을 REVIEW_REQUIRED 로 바꿀 수
있고, EVIDENCE_FOUND 로 바꾸지는 못한다.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.domain.models import (
    CriterionRule,
    EvidenceBundle,
    Observation,
    RuleOutcome,
)
from app.domain.states import CriterionKind, CriterionStatus
from app.main import app
from app.reasoning.supplements import SupplementBuilder
from app.reasoning.verifier import LocalEvidenceVerifier


# ---------------------------------------------------------------------------
# 테스트 더블
# ---------------------------------------------------------------------------


@dataclass
class FakeAnswer:
    """run_store.Answer 의 승격에 필요한 부분만 갖춘 대역."""

    answer_id: str
    criterion_id: str = "C-01"
    events: list[dict[str, Any]] = field(default_factory=list)


def event(**overrides: Any) -> dict[str, Any]:
    """정규화된 이벤트 한 건. 기본값은 승격 가능한 상태다."""
    base = {
        "event_type": "MEASUREMENT",
        "term": "hba1c",
        "label": "HbA1c",
        "value": 7.2,
        "unit": "%",
        "occurred_at": "2024-05-01",
        "date_precision": "DAY",
        "field": "hba1c",
        "source_span": "HbA1c 7.2%",
        "confidence": 0.9,
        "needs_review": False,
        "origin": "rule",
        "notes": [],
    }
    base.update(overrides)
    return base


def rule_for(field_name: str, *, kind: CriterionKind, unit: str | None) -> CriterionRule:
    return CriterionRule(
        criterion_id="C-01",
        criterion_type="INCLUSION",
        field_name=field_name,
        operator=">=",
        value_low="7.0",
        value_high=None,
        unit=unit,
        label=field_name,
        kind=kind,
        trial_id="T-01",
        criteria_version="v1",
    )


# ---------------------------------------------------------------------------
# 승격 규칙
# ---------------------------------------------------------------------------


def test_기준필드로_연결된_측정값은_승격된다() -> None:
    answers = [FakeAnswer("ANS-1", events=[event()])]
    result = SupplementBuilder().from_answers(answers)

    assert set(result.items) == {"hba1c"}
    observation = result.observations["hba1c"]
    assert observation.value == 7.2
    assert observation.unit == "%"
    assert observation.observed_at == "2024-05-01"
    assert observation.source == "PATIENT_REPORTED"
    assert observation.source_id == "ANS-1"
    assert observation.detail == "HbA1c 7.2%"


def test_상태_이벤트도_승격된다() -> None:
    answers = [
        FakeAnswer(
            "ANS-1",
            events=[
                event(
                    event_type="CONDITION",
                    term="active_pregnancy",
                    field="active_pregnancy",
                    value=True,
                    unit="boolean",
                    occurred_at=None,
                )
            ],
        )
    ]
    result = SupplementBuilder().from_answers(answers)
    assert result.observations["active_pregnancy"].value is True


@pytest.mark.parametrize(
    ("overrides", "reason_fragment"),
    [
        ({"event_type": "MEDICATION"}, "유형이 아닙니다"),
        ({"event_type": "ADVERSE_EVENT"}, "유형이 아닙니다"),
        ({"field": None}, "등록된 기준 필드가 아닙니다"),
        ({"value": None}, "값이 없습니다"),
        ({"needs_review": True}, "검토가 필요한"),
    ],
)
def test_승격하지_않는_경우와_이유(
    overrides: dict[str, Any], reason_fragment: str
) -> None:
    answers = [FakeAnswer("ANS-1", events=[event(**overrides)])]
    result = SupplementBuilder().from_answers(answers)

    assert result.items == {}
    assert len(result.skipped) == 1
    assert reason_fragment in result.skipped[0]["reason"]


def test_같은_필드는_측정시점이_늦은_값을_쓴다() -> None:
    answers = [
        FakeAnswer("ANS-1", events=[event(value=7.0, occurred_at="2024-01-01")]),
        FakeAnswer("ANS-2", events=[event(value=8.0, occurred_at="2024-05-01")]),
    ]
    result = SupplementBuilder().from_answers(answers)
    assert result.observations["hba1c"].value == 8.0
    assert result.observations["hba1c"].source_id == "ANS-2"


def test_시점이_늦은_값이_먼저_와도_유지된다() -> None:
    answers = [
        FakeAnswer("ANS-1", events=[event(value=8.0, occurred_at="2024-05-01")]),
        FakeAnswer("ANS-2", events=[event(value=7.0, occurred_at="2024-01-01")]),
    ]
    result = SupplementBuilder().from_answers(answers)
    assert result.observations["hba1c"].value == 8.0
    assert any("더 최신인 답변" in item["reason"] for item in result.skipped)


def test_시점이_같으면_확신도로_가른다() -> None:
    answers = [
        FakeAnswer("ANS-1", events=[event(value=7.0, confidence=0.7)]),
        FakeAnswer("ANS-2", events=[event(value=8.0, confidence=0.9)]),
    ]
    result = SupplementBuilder().from_answers(answers)
    assert result.observations["hba1c"].value == 8.0


def test_이벤트가_없는_답변은_조용히_넘어간다() -> None:
    result = SupplementBuilder().from_answers([FakeAnswer("ANS-1")])
    assert result.items == {}
    assert result.skipped == ()


# ---------------------------------------------------------------------------
# 검증기: 참여자 진술은 확정하지 않는다
# ---------------------------------------------------------------------------


def _verify(source: str, *, satisfied: bool) -> Any:
    rule = rule_for("hba1c", kind=CriterionKind.NUMERIC_POINT, unit="%")
    observation = Observation(
        field_name="hba1c",
        value=7.2,
        unit="%",
        observed_at="2024-05-01",
        source=source,  # type: ignore[arg-type]
        source_id="SRC-1",
    )
    bundle = EvidenceBundle(
        rule=rule,
        observations=[observation],
        outcome=RuleOutcome(
            criterion_id="C-01",
            satisfied=satisfied,
            observed_repr="7.2",
            expected_repr=">=7.0",
            explanation="규칙 계산 완료",
        ),
    )
    return LocalEvidenceVerifier().verify(bundle)


def test_기록으로_확인된_값은_충족을_확정한다() -> None:
    verification = _verify("TIMELINE_GRAPH", satisfied=True)
    assert verification.proposed_status is CriterionStatus.EVIDENCE_FOUND


def test_참여자_진술은_충족이어도_검토로_올린다() -> None:
    verification = _verify("PATIENT_REPORTED", satisfied=True)
    assert verification.proposed_status is CriterionStatus.REVIEW_REQUIRED
    assert verification.confidence < 0.55
    assert any("참여자 진술" in note for note in verification.notes)


def test_참여자_진술은_불충족이어도_부적합을_확정하지_않는다() -> None:
    # 자기 보고 값으로 CONTRADICTED 를 확정하면 부적합이 사람 검토 없이 굳는다.
    verification = _verify("PATIENT_REPORTED", satisfied=False)
    assert verification.proposed_status is CriterionStatus.REVIEW_REQUIRED


# ---------------------------------------------------------------------------
# 재판정 API
# ---------------------------------------------------------------------------


TRIAL = "SYN-DKD-01"
PERSON = 3
GAP_FIELD = "egfr"
"""SYN-DKD-01 의 DKD-C03 기준 필드. 이 값을 비워 미해소 상황을 만든다."""


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as instance:
        yield instance


@contextmanager
def missing_field(client: TestClient, field_name: str):
    """타임라인 조회에서 특정 필드를 빼 기록 공백을 만든다.

    합성 데이터셋은 모든 필드가 채워져 있어 UNKNOWN 이 발생하지 않는다.
    실제 EMR 의 결측 상황을 재현하려면 조회 결과에서 필드를 덜어내야 한다.
    """
    tool = client.app.state.container.timeline_tool
    original = tool.invoke

    def filtered(context, /, **kwargs):
        result = original(context, **kwargs)
        return {name: value for name, value in result.items() if name != field_name}

    tool.invoke = filtered
    try:
        yield
    finally:
        tool.invoke = original


def status_of(body: dict[str, Any], field_name: str) -> str:
    item = next(
        item for item in body["packet"]["items"] if item["field"] == field_name
    )
    return item["status"]


def test_답변_없이_재판정하면_거부한다(client: TestClient) -> None:
    run = client.post(
        "/api/v1/screening/run",
        json={"person_id": PERSON, "trial_id": TRIAL},
    ).json()
    response = client.post(f"/api/v1/screening/{run['run_id']}/rerun")
    assert response.status_code == 409


def test_없는_실행을_재판정하면_404(client: TestClient) -> None:
    assert client.post("/api/v1/screening/RUN-NOPE/rerun").status_code == 404


def test_일반_실행은_supplements가_비어있다(client: TestClient) -> None:
    body = client.post(
        "/api/v1/screening/run",
        json={"person_id": PERSON, "trial_id": TRIAL},
    ).json()
    assert body.get("supplements") is None


def test_기록에_값이_없으면_기준은_미해소로_남는다(client: TestClient) -> None:
    with missing_field(client, GAP_FIELD):
        body = client.post(
            "/api/v1/screening/run",
            json={"person_id": PERSON, "trial_id": TRIAL},
        ).json()
    assert status_of(body, GAP_FIELD) == "UNKNOWN"


def test_답변이_미해소_기준을_검토로_올린다(client: TestClient) -> None:
    with missing_field(client, GAP_FIELD):
        run = client.post(
            "/api/v1/screening/run",
            json={"person_id": PERSON, "trial_id": TRIAL},
        ).json()
        assert status_of(run, GAP_FIELD) == "UNKNOWN"

        target = next(
            item["criterion_id"]
            for item in run["packet"]["items"]
            if item["field"] == GAP_FIELD
        )
        answer = client.post(
            f"/api/v1/patients/{PERSON}/answers",
            json={
                "run_id": run["run_id"],
                "criterion_id": target,
                "value": "지난달 eGFR 48 mL/min/1.73m2 로 측정했습니다.",
            },
        ).json()
        # 정규화 결과가 답변에 함께 보관된다.
        assert any(
            item["field"] == GAP_FIELD for item in answer["intake"]["events"]
        )

        rerun = client.post(f"/api/v1/screening/{run['run_id']}/rerun")
        assert rerun.status_code == 200
        body = rerun.json()

    assert body["run_id"] != run["run_id"], "재판정은 새 실행으로 남는다"
    assert body["supplements"]["source_run_id"] == run["run_id"]
    assert GAP_FIELD in body["supplements"]["applied"]

    # 미해소를 벗어나되 충족으로 확정되지는 않는다.
    assert status_of(body, GAP_FIELD) == "REVIEW_REQUIRED"
    assert body["eligibility_status"] in ("REVIEW_REQUIRED", "INELIGIBLE")


def test_재판정은_감사_로그에_승격을_남긴다(client: TestClient) -> None:
    with missing_field(client, GAP_FIELD):
        run = client.post(
            "/api/v1/screening/run",
            json={"person_id": PERSON, "trial_id": TRIAL},
        ).json()
        target = next(
            item["criterion_id"]
            for item in run["packet"]["items"]
            if item["field"] == GAP_FIELD
        )
        client.post(
            f"/api/v1/patients/{PERSON}/answers",
            json={
                "run_id": run["run_id"],
                "criterion_id": target,
                "value": "지난달 eGFR 48 mL/min/1.73m2 로 측정했습니다.",
            },
        )
        body = client.post(f"/api/v1/screening/{run['run_id']}/rerun").json()

    events = client.get(f"/api/v1/audit/{body['run_id']}").json()
    applied = [item for item in events if item["action"] == "SUPPLEMENT_APPLIED"]
    assert applied, "승격 감사 이벤트가 없습니다."
    assert applied[0]["detail"]["fields"] == [GAP_FIELD]


def test_기록값이_있는_필드는_답변으로_덮어쓰지_않는다(client: TestClient) -> None:
    run = client.post(
        "/api/v1/screening/run",
        json={"person_id": PERSON, "trial_id": TRIAL},
    ).json()
    before = status_of(run, GAP_FIELD)
    target = next(
        item["criterion_id"]
        for item in run["packet"]["items"]
        if item["field"] == GAP_FIELD
    )

    client.post(
        f"/api/v1/patients/{PERSON}/answers",
        json={
            "run_id": run["run_id"],
            "criterion_id": target,
            "value": "지난달 eGFR 48 mL/min/1.73m2 로 측정했습니다.",
        },
    )
    body = client.post(f"/api/v1/screening/{run['run_id']}/rerun").json()

    assert GAP_FIELD not in body["supplements"]["applied"]
    ignored = {item["field"]: item["reason"] for item in body["supplements"]["ignored"]}
    assert "기록으로 확인된 값이 이미 있습니다." in ignored[GAP_FIELD]
    # 기록 기반 판정이 그대로 유지된다.
    assert status_of(body, GAP_FIELD) == before


def test_이_시험_기준에_없는_필드는_무시한다() -> None:
    from app.config import settings
    from app.container import build_container
    from app.domain.models import Observation

    container = build_container(settings.data_dir)
    output = container.orchestrator.run(
        person_id=PERSON,
        trial_id=TRIAL,
        supplements={
            "hba1c": Observation(
                field_name="hba1c",
                value=7.4,
                unit="%",
                observed_at="2024-05-01",
                source="PATIENT_REPORTED",
                source_id="ANS-x",
            )
        },
    )
    applied = output.run.metadata["supplements"]
    assert applied["applied"] == []
    assert applied["ignored"] == [
        {"field": "hba1c", "reason": "이 시험 기준에 없는 필드입니다."}
    ]
