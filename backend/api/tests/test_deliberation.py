"""UNKNOWN 제한형 교차 검토의 종료·그라운딩 계약."""

from __future__ import annotations

import json

from agent.deliberation import UnknownDeliberationAgent
from agent.model import ModelResponse
from app.domain.models import CriterionResult, NarrativeSnippet
from app.domain.states import CriterionKind, CriterionStatus
from app.safety.observability import TraceCollector


class ScriptedModel:
    mode = "scripted"

    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.calls = 0

    def converse(self, *, conversation, system, tools=None) -> ModelResponse:
        payload = self.payloads[self.calls]
        self.calls += 1
        return ModelResponse(
            text=json.dumps(payload, ensure_ascii=False),
            input_tokens=10,
            output_tokens=5,
        )


def _result(
    *,
    status: CriterionStatus = CriterionStatus.UNKNOWN,
    source_ids: tuple[str, ...] = ("NOTE-1",),
    criterion_id: str = "T-C01",
    criterion_type: str = "INCLUSION",
) -> CriterionResult:
    return CriterionResult(
        criterion_id=criterion_id,
        criterion_type=criterion_type,
        label="최근 HbA1c",
        field_name="hba1c",
        kind=CriterionKind.NUMERIC_POINT,
        status=status,
        observed_value="7.8",
        expected_condition="7.5-10.5",
        unit="%",
        observed_at="2026-01-01",
        confidence=0.4,
        explanation="추가 검토가 필요합니다.",
        source_ids=source_ids,
        narrative=(
            NarrativeSnippet(
                note_id="NOTE-1",
                encounter_id="evt_1",
                note_date="2026-01-01",
                snippet="HbA1c 7.8%로 기록됨.",
                matched_terms=("HbA1c",),
                score=0.9,
            ),
        ),
    )


def _decision(recommendation: str, source_id: str = "NOTE-1") -> dict:
    return {
        "decisions": [
            {
                "criterion_id": "T-C01",
                "recommendation": recommendation,
                "source_ids": [source_id],
                "rationale": "기록된 수치와 기준을 비교했습니다.",
            }
        ]
    }


def test_grounded_consensus_produces_ok_recommendation() -> None:
    model = ScriptedModel([_decision("OK"), _decision("OK")])
    result = UnknownDeliberationAgent(
        model=model, trace=TraceCollector(), max_criteria=5
    ).deliberate([_result()], run_id="RUN-1")

    assert model.calls == 2
    assert result.rounds == result.max_rounds == 2
    assert result.stopped_reason == "bounded_consensus"
    assert result.items[0].recommendation == "OK"
    assert result.items[0].grounded is True


def test_disagreement_stops_after_two_rounds_and_keeps_unknown() -> None:
    model = ScriptedModel([_decision("OK"), _decision("NOT_OK")])
    result = UnknownDeliberationAgent(
        model=model, trace=TraceCollector()
    ).deliberate([_result()], run_id="RUN-2")

    assert model.calls == 2
    assert result.rounds == 2
    assert result.items[0].recommendation == "UNKNOWN"
    assert result.items[0].agreement is False


def test_fabricated_source_cannot_resolve_unknown() -> None:
    model = ScriptedModel(
        [_decision("NOT_OK", "MADE-UP"), _decision("NOT_OK", "MADE-UP")]
    )
    result = UnknownDeliberationAgent(
        model=model, trace=TraceCollector()
    ).deliberate([_result()], run_id="RUN-3")

    assert result.items[0].recommendation == "UNKNOWN"
    assert result.items[0].grounded is False
    assert result.items[0].source_ids == ()


def test_resolved_criteria_do_not_call_model() -> None:
    model = ScriptedModel([])
    result = UnknownDeliberationAgent(
        model=model, trace=TraceCollector()
    ).deliberate([_result(status=CriterionStatus.EVIDENCE_FOUND)])

    assert model.calls == 0
    assert result.rounds == 0
    assert result.stopped_reason == "nothing_to_review"


def test_deliberation_is_limited_to_two_rounds_and_five_criteria() -> None:
    results = [
        _result(criterion_id=f"INC-{index}") for index in range(1, 7)
    ] + [
        _result(criterion_id="EXC-1", criterion_type="EXCLUSION")
    ]
    decisions = {
        "decisions": [
            {
                "criterion_id": item.criterion_id,
                "recommendation": "OK",
                "source_ids": ["NOTE-1"],
                "rationale": "근거를 확인했습니다.",
            }
            for item in results
        ]
    }
    model = ScriptedModel([decisions, decisions])

    result = UnknownDeliberationAgent(
        model=model,
        trace=TraceCollector(),
        max_criteria=5,
    ).deliberate(results, run_id="RUN-LIMIT")

    assert model.calls == 2
    assert result.rounds == result.max_rounds == 2
    assert result.stopped_reason == "criteria_limit"
    assert len(result.items) == 5
    assert "EXC-1" in {item.criterion_id for item in result.items}
