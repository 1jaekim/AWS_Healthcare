"""가드레일 위반 시나리오 검증.

세 층에서 확인한다.

1. 규칙 자체: 각 규칙 ID가 실제로 걸리는지, 무엇으로 바뀌는지
2. 에이전트 경로: 모델이 위반 문장을 내면 어떻게 처리되는지
3. 감사 경로: 차단·완화 사실이 기록에 남는지

가드레일은 '차단'과 '완화' 두 가지로 동작한다. 완화는 문구를 바꿔 통과시키고,
차단은 아예 내보내지 않는다. 둘을 섞어 쓰면 위험 문구가 완화만 되고 나가므로
구분을 테스트로 고정한다.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.intake import IntakeAgent
from agent.model import ModelResponse
from agent.narration import ModelExplanationAgent, ModelNextBestEvidenceAgent
from app.actions.explanation import ExplanationAgent
from app.actions.next_best import NextBestEvidenceAgent
from app.domain.models import CriterionResult
from app.domain.states import CriterionKind, CriterionStatus, EligibilityStatus
from app.main import app
from app.reasoning.aggregator import AggregateOutcome
from app.safety.guardrails import GuardrailVerdict, LocalGuardrail
from app.safety.observability import TraceCollector

PATIENT_DISCLAIMER = "의료적 진단이나 치료 권고가 아닙니다"
ADMIN_DISCLAIMER = "연구 담당자의 검토가 필요합니다"


def bare() -> LocalGuardrail:
    """면책 문구를 붙이지 않는 가드레일. 완화 결과만 보기 위해 쓴다."""
    return LocalGuardrail(attach_disclaimer=False)


def rule_ids(verdict: GuardrailVerdict) -> set[str]:
    return {item.rule_id for item in verdict.findings}


# ---------------------------------------------------------------------------
# 완화 규칙: 문구를 바꿔 통과시킨다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule_id", "text", "removed", "added", "severity"),
    [
        (
            "GR-DIAG-01",
            "제2형 당뇨로 확진되었습니다.",
            "확진되었",
            "진단 기록이 확인",
            "HIGH",
        ),
        (
            "GR-DIAG-02",
            "확정 진단 기록이 있습니다.",
            "확정 진단",
            "기록상 진단",
            "HIGH",
        ),
        (
            "GR-TREAT-01",
            "지금 복용을 중단하세요.",
            "중단하세요",
            "담당 의료진과 상의",
            "HIGH",
        ),
        (
            "GR-TREAT-02",
            "처방을 변경하세요.",
            "변경하세요",
            "담당 의료진과 상의",
            "HIGH",
        ),
        (
            "GR-PROG-01",
            "이 치료로 완치됩니다.",
            "완치됩니다",
            "경과는 의료진이 판단",
            "HIGH",
        ),
        (
            "GR-ELIG-01",
            "연구 참여가 확정되었습니다.",
            "참여가 확정되었",
            "참여 가능성이 있다고 판단",
            "MEDIUM",
        ),
        (
            "GR-ELIG-02",
            "이 분은 반드시 적합합니다.",
            "반드시 적합",
            "적합 가능성이 높음",
            "MEDIUM",
        ),
        (
            "GR-ADVICE-01",
            "빨리 치료를 받으세요.",
            "치료를 받으세요",
            "의료진 상담을 권고",
            "MEDIUM",
        ),
    ],
)
def test_확정_표현은_완화되어_통과한다(
    rule_id: str, text: str, removed: str, added: str, severity: str
) -> None:
    verdict = bare().review(text, audience="admin")

    assert rule_id in rule_ids(verdict), f"{rule_id} 가 걸리지 않았습니다."
    assert not verdict.blocked, "완화 규칙은 차단하지 않는다."
    assert removed not in verdict.text, f"위험 표현이 남아 있습니다: {removed}"
    assert added in verdict.text
    assert verdict.modified

    finding = next(item for item in verdict.findings if item.rule_id == rule_id)
    assert finding.severity == severity


def test_한_문장에_여러_위반이_있으면_모두_기록한다() -> None:
    verdict = bare().review(
        "확진되었고 완치됩니다. 복용을 중단하세요.", audience="patient"
    )
    assert {"GR-DIAG-01", "GR-PROG-01", "GR-TREAT-01"} <= rule_ids(verdict)
    assert not verdict.blocked


def test_같은_규칙이_여러_번_나오면_각각_기록한다() -> None:
    verdict = bare().review("확진되었습니다. 다시 확진되었습니다.", audience="admin")
    hits = [item for item in verdict.findings if item.rule_id == "GR-DIAG-01"]
    assert len(hits) == 2


# ---------------------------------------------------------------------------
# 차단 규칙: 내보내지 않는다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule_id", "text"),
    [
        ("GR-BLOCK-01", "제가 진단을 대신 해드리겠습니다."),
        ("GR-BLOCK-01", "처방을 대신 하겠습니다."),
        ("GR-BLOCK-02", "의사 없이 복용하셔도 됩니다."),
        ("GR-BLOCK-02", "의사 없이 중단하세요."),
        ("GR-BLOCK-02", "의사 없이 투여해도 괜찮습니다."),
    ],
)
def test_위험_표현은_차단한다(rule_id: str, text: str) -> None:
    verdict = bare().review(text, audience="patient")

    assert verdict.blocked
    assert rule_id in rule_ids(verdict)
    assert verdict.text == "안전 정책에 따라 표시할 수 없는 내용입니다."
    assert all(item.severity == "CRITICAL" for item in verdict.findings)


def test_차단이_완화보다_먼저_적용된다() -> None:
    """차단 대상이면 완화를 시도하지 않고 즉시 막는다."""
    verdict = bare().review(
        "확진되었으니 의사 없이 복용하세요.", audience="patient"
    )
    assert verdict.blocked
    assert rule_ids(verdict) == {"GR-BLOCK-02"}, "차단 시 완화 규칙은 돌지 않는다"


def test_차단된_문구는_결과에_남지_않는다() -> None:
    verdict = bare().review("의사 없이 복용하셔도 됩니다.", audience="patient")
    assert "의사 없이" not in verdict.text


# ---------------------------------------------------------------------------
# 걸리지 않아야 하는 경우
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "HbA1c 7.2% 로 기준을 충족합니다.",
        "관찰값이 확인되지 않아 추가 확인이 필요합니다.",
        "미확정 진단 상태입니다.",
        "복용 중인 약을 담당 의료진과 상의하세요.",
        "치료 이력을 확인했습니다.",
        "참여 가능성을 검토 중입니다.",
    ],
)
def test_정상_문장은_건드리지_않는다(text: str) -> None:
    verdict = bare().review(text, audience="admin")
    assert not verdict.blocked
    assert verdict.findings == []
    assert verdict.text == text
    assert not verdict.modified


def test_미확정_진단은_확정_표현으로_보지_않는다() -> None:
    """'미확정 진단' 은 부정 표현이다. 부정 전방탐색이 동작해야 한다."""
    verdict = bare().review("미확정 진단 상태입니다.", audience="admin")
    assert "GR-DIAG-02" not in rule_ids(verdict)


# ---------------------------------------------------------------------------
# 근거 없는 생성 차단 (Contextual Grounding)
# ---------------------------------------------------------------------------


def test_출처가_없으면_설명_생성을_막는다() -> None:
    verdict = bare().check_grounding("설명 생성 요청", source_ids=[])
    assert verdict.blocked
    assert rule_ids(verdict) == {"GR-GROUND-01"}
    assert "근거 출처가 확인되지 않아" in verdict.text


def test_출처가_있으면_통과시킨다() -> None:
    verdict = bare().check_grounding("설명 생성 요청", source_ids=["ENC-1"])
    assert not verdict.blocked
    assert verdict.findings == []


# ---------------------------------------------------------------------------
# 면책 문구
# ---------------------------------------------------------------------------


def test_대상별로_다른_면책_문구를_붙인다() -> None:
    guardrail = LocalGuardrail()
    assert PATIENT_DISCLAIMER in guardrail.review("확인했습니다.", audience="patient").text
    assert ADMIN_DISCLAIMER in guardrail.review("확인했습니다.", audience="admin").text


def test_면책_문구를_두_번_붙이지_않는다() -> None:
    guardrail = LocalGuardrail()
    once = guardrail.review("확인했습니다.", audience="patient").text
    twice = guardrail.review(once, audience="patient").text
    assert twice.count(PATIENT_DISCLAIMER) == 1


def test_차단된_문구에는_면책을_붙이지_않는다() -> None:
    guardrail = LocalGuardrail()
    verdict = guardrail.review("의사 없이 복용하세요.", audience="patient")
    assert verdict.blocked
    assert PATIENT_DISCLAIMER not in verdict.text


# ---------------------------------------------------------------------------
# 에이전트 경로: 모델이 위반 문장을 낼 때
# ---------------------------------------------------------------------------


def outcome() -> AggregateOutcome:
    return AggregateOutcome(
        eligibility_status=EligibilityStatus.NEEDS_MORE_EVIDENCE,
        decision_label="추가 근거 필요",
        criteria_total=2,
        criteria_met=1,
        blocking_criteria=(),
        open_criteria=("C-02",),
        review_criteria=(),
    )


def results() -> list[CriterionResult]:
    return [
        CriterionResult(
            criterion_id="C-01",
            criterion_type="INCLUSION",
            label="HbA1c",
            field_name="hba1c",
            kind=CriterionKind.NUMERIC_POINT,
            status=CriterionStatus.EVIDENCE_FOUND,
            observed_value="7.2",
            expected_condition=">=7.0",
            unit="%",
            observed_at="2024-05-01",
            confidence=0.9,
            explanation="충족",
            source_ids=("ENC-1",),
        ),
        CriterionResult(
            criterion_id="C-02",
            criterion_type="INCLUSION",
            label="eGFR",
            field_name="egfr",
            kind=CriterionKind.NUMERIC_POINT,
            status=CriterionStatus.UNKNOWN,
            observed_value=None,
            expected_condition=">=25",
            unit="mL/min/1.73m2",
            observed_at=None,
            confidence=0.0,
            explanation="관찰값 없음",
            source_ids=("ENC-2",),
        ),
    ]


class ScriptedModel:
    """정해진 JSON 문자열을 돌려주는 모델."""

    mode = "scripted"

    def __init__(self, text: str) -> None:
        self._text = text

    def converse(self, *, conversation, system, tools=None):
        return ModelResponse(text=self._text)


def explainer(text: str) -> ModelExplanationAgent:
    guardrail = LocalGuardrail()
    return ModelExplanationAgent(
        model=ScriptedModel(text),
        guardrail=guardrail,
        fallback=ExplanationAgent(guardrail),
        trace=TraceCollector(),
    )


def test_모델이_확정_표현을_쓰면_완화해서_내보낸다() -> None:
    agent = explainer(
        '{"summary": "제2형 당뇨로 확진되었습니다.", "highlights": ["완치됩니다."]}'
    )
    explanation = agent.explain(
        outcome=outcome(), results=results(), audience="admin"
    )

    assert not explanation.blocked
    assert "확진되었" not in explanation.summary
    assert "진단 기록이 확인" in explanation.summary
    found = {item["rule_id"] for item in explanation.guardrail_findings}
    assert "GR-DIAG-01" in found
    assert all("완치됩니다" not in line for line in explanation.highlights)


def test_모델이_위험_표현을_쓰면_설명을_막는다() -> None:
    agent = explainer(
        '{"summary": "의사 없이 복용하셔도 됩니다.", "highlights": ["괜찮습니다."]}'
    )
    explanation = agent.explain(
        outcome=outcome(), results=results(), audience="patient"
    )

    assert explanation.blocked
    assert explanation.highlights == []
    assert "의사 없이" not in explanation.summary
    found = {item["rule_id"] for item in explanation.guardrail_findings}
    assert "GR-BLOCK-02" in found


def test_차단된_항목만_빼고_나머지_항목은_남긴다() -> None:
    agent = explainer(
        '{"summary": "확인이 필요합니다.", '
        '"highlights": ["의사 없이 복용하세요.", "eGFR 확인이 필요합니다."]}'
    )
    explanation = agent.explain(
        outcome=outcome(), results=results(), audience="admin"
    )

    assert not explanation.blocked
    assert len(explanation.highlights) == 1
    assert "eGFR" in explanation.highlights[0]


def test_출처가_없으면_모델을_부르지_않고_템플릿으로_답한다() -> None:
    """Contextual Grounding 선결 조건. 위반 문장을 만들 기회 자체를 없앤다."""

    class Exploding:
        mode = "exploding"

        def converse(self, *, conversation, system, tools=None):
            raise AssertionError("출처가 없으면 모델을 호출하면 안 된다")

    guardrail = LocalGuardrail()
    agent = ModelExplanationAgent(
        model=Exploding(),
        guardrail=guardrail,
        fallback=ExplanationAgent(guardrail),
        trace=TraceCollector(),
    )
    ungrounded = [
        CriterionResult(**{**results()[0].__dict__, "source_ids": ()}),
        CriterionResult(**{**results()[1].__dict__, "source_ids": ()}),
    ]
    explanation = agent.explain(
        outcome=outcome(), results=ungrounded, audience="patient"
    )
    assert explanation.blocked


def test_모델_질문이_차단되면_템플릿_질문으로_돌아간다() -> None:
    guardrail = bare()
    fallback = NextBestEvidenceAgent(guardrail)
    agent = ModelNextBestEvidenceAgent(
        model=ScriptedModel('{"question": "의사 없이 복용하셔도 되나요?"}'),
        guardrail=guardrail,
        fallback=fallback,
        trace=TraceCollector(),
    )
    proposed = agent.propose("RUN-G", results())

    assert proposed, "미해소 기준이 있으면 질문이 나와야 한다"
    for request in proposed:
        assert "의사 없이" not in request.question
    # 템플릿 질문과 동일해야 한다.
    baseline = {item.criterion_id: item.question for item in fallback.propose("RUN-G", results())}
    assert {item.criterion_id: item.question for item in proposed} == baseline


def test_모델_질문의_확정_표현은_완화된다() -> None:
    guardrail = bare()
    agent = ModelNextBestEvidenceAgent(
        model=ScriptedModel('{"question": "참여가 확정되었나요?"}'),
        guardrail=guardrail,
        fallback=NextBestEvidenceAgent(guardrail),
        trace=TraceCollector(),
    )
    proposed = agent.propose("RUN-G", results())
    assert proposed
    assert all("확정되었" not in item.question for item in proposed)


def test_intake는_차단된_용어를_버린다() -> None:
    """모델이 위험 표현을 라벨로 넣어도 이벤트로 남지 않는다."""

    class Injecting:
        mode = "injecting"

        def converse(self, *, conversation, system, tools=None):
            return ModelResponse(
                text='{"events": [{"type": "ADVERSE_EVENT", '
                '"term": "의사 없이 복용", "value": true, '
                '"span": "의사 없이 복용", "confidence": 0.9}]}'
            )

    agent = IntakeAgent(
        guardrail=bare(), trace=TraceCollector(), model=Injecting()
    )
    result = agent.normalize("의사 없이 복용해도 되나요?", reference_date="2024-06-15")

    assert all(item.origin != "model" for item in result.events)
    assert any(
        "안전 정책" in item["reason"] for item in result.dropped
    ), f"차단 사유가 남지 않았습니다: {result.dropped}"


# ---------------------------------------------------------------------------
# 감사 경로
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as instance:
        yield instance


def test_실행_결과_설명에_면책_문구가_붙는다(client: TestClient) -> None:
    body = client.post(
        "/api/v1/screening/run",
        json={"person_id": 3, "trial_id": "SYN-DKD-01"},
    ).json()
    assert ADMIN_DISCLAIMER in body["explanations"]["admin"]["summary"]
    assert PATIENT_DISCLAIMER in body["explanations"]["patient"]["summary"]
    assert not body["explanations"]["admin"]["blocked"]


def test_위반이_없는_실행은_감사_이벤트도_없다(client: TestClient) -> None:
    """결정론적 경로의 템플릿 문장은 확정 표현을 쓰지 않는다."""
    body = client.post(
        "/api/v1/screening/run",
        json={"person_id": 3, "trial_id": "SYN-DKD-01"},
    ).json()
    assert body["explanations"]["admin"]["guardrail_findings"] == []

    events = client.get(f"/api/v1/audit/{body['run_id']}").json()
    assert [item for item in events if item["action"] == "GUARDRAIL_TRIGGERED"] == []


def violating_run() -> tuple[Any, Any]:
    """모델이 확정 표현을 내는 실행을 만든다.

    템플릿 경로는 위반을 만들지 않으므로, 감사 경로를 실제로 태우려면 모델을 꽂아야 한다.
    """
    import json as _json

    from app.config import settings
    from app.container import build_container

    class Violating:
        mode = "violating"

        def converse(self, *, conversation, system, tools=None):
            return ModelResponse(
                text=_json.dumps(
                    {
                        "summary": "제2형 당뇨로 확진되었습니다.",
                        "highlights": ["이 치료로 완치됩니다."],
                    },
                    ensure_ascii=False,
                )
            )

    container = build_container(settings.data_dir, model_client=Violating())
    output = container.orchestrator.run(person_id=3, trial_id="SYN-DKD-01")
    return container, output


def test_가드레일_완화가_감사_로그에_남는다() -> None:
    """무엇을 왜 바꿨는지 되짚을 수 있어야 한다."""
    container, output = violating_run()

    # 완화가 실제로 일어났는지 먼저 확인한다. 여기가 비면 아래 검증이 공허해진다.
    findings = output.explanations["admin"]["guardrail_findings"]
    assert findings, "완화가 일어나지 않아 감사 검증이 성립하지 않습니다."
    assert "확진되었" not in output.explanations["admin"]["summary"]

    events = container.audit.for_run(output.run.run_id)
    triggered = [item for item in events if item["action"] == "GUARDRAIL_TRIGGERED"]
    assert len(triggered) == 2, "관리자·참여자 두 건이 남아야 한다"

    audiences = {item["detail"]["audience"] for item in triggered}
    assert audiences == {"admin", "patient"}
    for item in triggered:
        assert "GR-DIAG-01" in item["detail"]["rule_ids"]
        assert item["detail"]["severities"] == ["HIGH"]
        assert item["detail"]["finding_count"] >= 1
        assert item["detail"]["blocked"] is False


def test_감사_로그에_원문을_남기지_않는다() -> None:
    """차단·완화된 문구가 감사 로그로 새어나가면 안 된다."""
    container, output = violating_run()
    events = container.audit.for_run(output.run.run_id)
    triggered = [item for item in events if item["action"] == "GUARDRAIL_TRIGGERED"]
    assert triggered

    for item in triggered:
        assert set(item["detail"]) == {
            "audience",
            "blocked",
            "rule_ids",
            "severities",
            "finding_count",
        }
        serialized = str(item["detail"])
        assert "확진" not in serialized
        assert "완치" not in serialized


def test_모든_규칙_ID가_테스트로_덮여있다() -> None:
    """규칙을 추가하면 시나리오도 함께 추가하게 만든다."""
    from app.safety import guardrails as module

    declared = {rule_id for rule_id, *_ in module._CERTAINTY_RULES}
    declared |= {rule_id for rule_id, _ in module._BLOCK_RULES}
    declared.add("GR-GROUND-01")

    covered = {
        "GR-DIAG-01",
        "GR-DIAG-02",
        "GR-TREAT-01",
        "GR-TREAT-02",
        "GR-PROG-01",
        "GR-ELIG-01",
        "GR-ELIG-02",
        "GR-ADVICE-01",
        "GR-BLOCK-01",
        "GR-BLOCK-02",
        "GR-GROUND-01",
    }
    assert declared == covered, (
        f"테스트가 없는 규칙: {sorted(declared - covered)} / "
        f"사라진 규칙: {sorted(covered - declared)}"
    )
