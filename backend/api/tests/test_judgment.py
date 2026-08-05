"""LLM 판단 → Verifier → Rule Aggregator 계약 검증.

확인하려는 것은 두 가지다.

- 모델이 무엇을 제안하든 상태 확정은 결정론적 계층이 한다.
- 검증 항목 실패는 조용히 넘어가지 않고 상태와 경로에 반영된다.
"""

from __future__ import annotations

import json

import pytest

from agent.judge import CriterionJudgeAgent, CriterionJudgment
from agent.model import ModelError, ModelResponse
from app.domain.models import (
    CriterionRule,
    EvidenceBundle,
    NarrativeSnippet,
    Observation,
    RuleOutcome,
    Verification,
)
from app.domain.states import CriterionKind, CriterionStatus
from app.reasoning.judgment import JudgmentVerifier
from app.reasoning.rule_aggregator import A2A, DECIDED, HUMAN_REVIEW, RuleAggregator
from app.reasoning.verifier import LocalEvidenceVerifier
from app.safety.observability import TraceCollector

INDEX_DATE = "2026-08-04"


class ScriptedModel:
    """정해진 JSON 을 순서대로 돌려주는 모델."""

    mode = "scripted"

    def __init__(self, payloads: list[dict | ModelResponse]) -> None:
        self.payloads = list(payloads)
        self.requests: list[dict] = []

    def converse(self, *, conversation, system, tools=None) -> ModelResponse:
        self.requests.append(
            {"system": str(system), "messages": list(conversation.messages)}
        )
        payload = self.payloads.pop(0)
        if isinstance(payload, ModelResponse):
            return payload
        return ModelResponse(
            text=json.dumps(payload, ensure_ascii=False),
            input_tokens=11,
            output_tokens=7,
        )


class FailingModel:
    mode = "failing"

    def converse(self, *, conversation, system, tools=None):
        raise ModelError("의도적 실패")


# ---------------------------------------------------------------------------
# 픽스처 도우미
# ---------------------------------------------------------------------------


def _rule(
    *,
    criterion_type: str = "INCLUSION",
    field_name: str = "hba1c",
    operator: str = "between",
    value_low: str | None = "7.5",
    value_high: str | None = "10.5",
    unit: str | None = "%",
    time_window_days: int | None = None,
    kind: CriterionKind = CriterionKind.NUMERIC_POINT,
) -> CriterionRule:
    return CriterionRule(
        criterion_id="C01",
        criterion_type=criterion_type,
        field_name=field_name,
        operator=operator,
        value_low=value_low,
        value_high=value_high,
        unit=unit,
        label="HbA1c",
        kind=kind,
        trial_id="T",
        criteria_version="v1-test",
        time_window_days=time_window_days,
    )


def _bundle(
    *,
    satisfied: bool | None = True,
    value: float | bool | None = 8.2,
    observed_at: str | None = INDEX_DATE,
    unit: str | None = "%",
    rule: CriterionRule | None = None,
    narrative: bool = False,
) -> EvidenceBundle:
    bundle = EvidenceBundle(rule=rule or _rule())
    if value is not None:
        bundle.observations.append(
            Observation(
                field_name=bundle.rule.field_name,
                value=value,
                unit=unit,
                observed_at=observed_at,
                source="TIMELINE_GRAPH",
                source_id="ENC-1",
                detail="HbA1c 8.2%",
            )
        )
    if narrative:
        bundle.narrative.append(
            NarrativeSnippet(
                note_id="NOTE-ENC-1",
                encounter_id="ENC-1",
                note_date=INDEX_DATE,
                snippet="HbA1c 8.2% 로 기록됨.",
                matched_terms=("HbA1c",),
                score=0.9,
            )
        )
    bundle.outcome = RuleOutcome(
        criterion_id=bundle.rule.criterion_id,
        satisfied=satisfied,
        observed_repr=None if value is None else str(value),
        expected_repr=bundle.rule.expected_repr(),
        explanation="규칙 계산 결과입니다.",
    )
    return bundle


def _rule_verification(
    status: CriterionStatus = CriterionStatus.EVIDENCE_FOUND,
    *,
    confidence: float = 0.9,
    conflicts: tuple[str, ...] = (),
) -> Verification:
    return Verification(
        criterion_id="C01",
        proposed_status=status,
        confidence=confidence,
        grounded=True,
        conflicts=conflicts,
    )


def _judgment(
    status: str,
    *,
    confidence: float = 0.9,
    evidence: tuple[str, ...] = ("ENC-1",),
    reason: str = "근거를 확인했습니다.",
    missing: tuple[str, ...] = (),
    needs_a2a: bool = False,
    origin: str = "model",
) -> CriterionJudgment:
    return CriterionJudgment(
        criterion_id="C01",
        proposed_status=status,
        confidence=confidence,
        reason=reason,
        used_evidence_ids=evidence,
        missing_information=missing,
        needs_a2a=needs_a2a,
        origin=origin,
    )


def _confirm(
    bundle: EvidenceBundle,
    judgment: CriterionJudgment,
    rule_verification: Verification | None = None,
    *,
    index_date: str | None = INDEX_DATE,
):
    """Verifier → Rule Aggregator 를 한 번에 돌린다."""
    verification = rule_verification or _rule_verification()
    report = JudgmentVerifier().verify(
        bundle, judgment, rule_verification=verification, index_date=index_date
    )
    decision = RuleAggregator().confirm(
        bundle,
        judgment=judgment,
        report=report,
        rule_verification=verification,
    )
    return decision, report


def _judge(model) -> CriterionJudgeAgent:
    return CriterionJudgeAgent(model=model, trace=TraceCollector())


# ---------------------------------------------------------------------------
# LLM 판단: 입력 계약
# ---------------------------------------------------------------------------


def test_판단_입력은_v2_계약을_따른다() -> None:
    """공고 기준의 연산자·값·단위·기간과 근거 목록이 모두 실린다."""
    model = ScriptedModel([{"proposed_status": "OK", "confidence": 0.9}])
    bundle = _bundle(rule=_rule(time_window_days=180), narrative=True)

    _judge(model).judge(
        bundle,
        rule_verification=_rule_verification(),
        run_id="RUN-1",
        patient_key="pt_7f3a",
        index_date=INDEX_DATE,
    )

    sent = json.loads(
        model.requests[0]["messages"][0]["content"][0]["text"].split("\n\n", 1)[1]
    )
    assert sent["task"] == "evaluate_trial_criterion"
    assert sent["patient_key"] == "pt_7f3a"
    assert sent["allowed_status"] == ["OK", "NOT_OK", "UNKNOWN"]
    assert sent["criterion"]["operator"] == "between"
    assert sent["criterion"]["unit"] == "%"
    assert sent["criterion"]["time_window_days"] == 180
    assert [item["evidence_id"] for item in sent["evidence"]] == [
        "ENC-1",
        "NOTE-ENC-1",
    ]


def test_직접_식별자는_판단_입력에_들어가지_않는다() -> None:
    """LLM 에는 가명 키만 넘긴다."""
    model = ScriptedModel([{"proposed_status": "OK", "confidence": 0.9}])
    _judge(model).judge(
        _bundle(),
        rule_verification=_rule_verification(),
        run_id="RUN-1",
        patient_key="pt_7f3a",
    )
    sent = model.requests[0]["messages"][0]["content"][0]["text"]
    assert "person_id" not in sent
    assert "pt_7f3a" in sent


def test_근거가_없으면_모델을_부르지_않는다() -> None:
    model = ScriptedModel([])
    judgment = _judge(model).judge(
        _bundle(satisfied=None, value=None),
        rule_verification=_rule_verification(CriterionStatus.UNKNOWN, confidence=0.0),
    )
    assert model.requests == []
    assert judgment.origin == "rule"
    assert judgment.proposed_status == "UNKNOWN"


def test_여러_기준은_모델_한번으로_일괄_판단한다() -> None:
    second_rule = _rule(field_name="bmi")
    object.__setattr__(second_rule, "criterion_id", "C02")
    model = ScriptedModel(
        [{
            "judgments": [
                {"criterion_id": "C01", "proposed_status": "OK", "confidence": 0.9},
                {"criterion_id": "C02", "proposed_status": "NOT_OK", "confidence": 0.8},
            ]
        }]
    )
    judgments = _judge(model).judge_many(
        [
            (_bundle(), _rule_verification()),
            (_bundle(rule=second_rule), _rule_verification()),
        ],
        run_id="RUN-BATCH",
    )

    assert len(model.requests) == 1
    sent = json.loads(
        model.requests[0]["messages"][0]["content"][0]["text"].split("\n\n", 1)[1]
    )
    assert sent["task"] == "evaluate_trial_criteria_batch"
    assert [item["criterion"]["criterion_id"] for item in sent["items"]] == [
        "C01", "C02"
    ]
    assert [item.proposed_status for item in judgments] == ["OK", "NOT_OK"]
    assert sum(item.input_tokens for item in judgments) == 11


# ---------------------------------------------------------------------------
# LLM 판단: 폴백
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payloads",
    [
        [ModelResponse(text="JSON 이 아닙니다")],
        [{"proposed_status": "MAYBE", "confidence": 0.9}],
    ],
    ids=["형식_오류", "허용되지_않은_상태"],
)
def test_모델_응답이_계약을_벗어나면_규칙_판정을_승계한다(payloads) -> None:
    judgment = _judge(ScriptedModel(payloads)).judge(
        _bundle(satisfied=True),
        rule_verification=_rule_verification(),
    )
    assert judgment.origin == "rule"
    assert judgment.proposed_status == "OK"
    assert judgment.error


def test_모델_호출_실패는_규칙_판정을_승계한다() -> None:
    judgment = _judge(FailingModel()).judge(
        _bundle(satisfied=False),
        rule_verification=_rule_verification(CriterionStatus.CONTRADICTED),
    )
    assert judgment.origin == "rule"
    assert judgment.proposed_status == "NOT_OK"
    assert "의도적 실패" in (judgment.error or "")


def test_모델이_없으면_규칙_판정을_승계한다() -> None:
    judgment = _judge(None).judge(
        _bundle(satisfied=True), rule_verification=_rule_verification()
    )
    assert judgment.origin == "rule"
    assert judgment.model_backed is False
    assert judgment.proposed_status == "OK"


# ---------------------------------------------------------------------------
# Verifier: 일곱 가지 검증 항목
# ---------------------------------------------------------------------------


def _check(report, check_id: str):
    return next(item for item in report.checks if item.check_id == check_id)


def test_모든_검증_항목이_기록된다() -> None:
    _, report = _confirm(_bundle(), _judgment("OK"))
    assert [item.check_id for item in report.checks] == [
        "V-EVIDENCE",
        "V-WINDOW",
        "V-UNIT",
        "V-OPERATOR",
        "V-TYPE",
        "V-PII",
        "V-CONFIDENCE",
    ]
    assert report.passed is True
    assert report.grounded is True


def test_없는_출처를_인용하면_근거_검증이_실패한다() -> None:
    decision, report = _confirm(_bundle(), _judgment("OK", evidence=("MADE-UP",)))
    assert _check(report, "V-EVIDENCE").passed is False
    assert report.fabricated_evidence_ids == ("MADE-UP",)
    assert report.grounded is False
    assert decision.status == "UNKNOWN"
    assert decision.route == HUMAN_REVIEW


def test_출처_인용_없이는_확정하지_못한다() -> None:
    decision, report = _confirm(_bundle(), _judgment("OK", evidence=()))
    assert _check(report, "V-EVIDENCE").passed is False
    assert decision.status == "UNKNOWN"


def test_기준_기간을_벗어난_관찰은_확정하지_못한다() -> None:
    bundle = _bundle(rule=_rule(time_window_days=180), observed_at="2025-01-01")
    decision, report = _confirm(bundle, _judgment("OK"))
    assert _check(report, "V-WINDOW").passed is False
    assert "기준 기간" in _check(report, "V-WINDOW").detail
    assert decision.status == "UNKNOWN"


def test_기간_안의_관찰은_통과한다() -> None:
    bundle = _bundle(rule=_rule(time_window_days=180), observed_at="2026-07-20")
    decision, report = _confirm(bundle, _judgment("OK"))
    assert _check(report, "V-WINDOW").passed is True
    assert decision.status == "OK"


def test_관찰_시점이_없으면_확정하지_못한다() -> None:
    decision, report = _confirm(_bundle(observed_at=None), _judgment("OK"))
    assert _check(report, "V-WINDOW").passed is False
    assert decision.status == "UNKNOWN"


def test_미래_시점_관찰은_확정하지_못한다() -> None:
    bundle = _bundle(rule=_rule(time_window_days=180), observed_at="2026-12-31")
    _, report = _confirm(bundle, _judgment("OK"))
    assert _check(report, "V-WINDOW").passed is False
    assert "미래" in _check(report, "V-WINDOW").detail


def test_단위가_다르면_확정하지_못한다() -> None:
    decision, report = _confirm(_bundle(unit="mmol/mol"), _judgment("OK"))
    assert _check(report, "V-UNIT").passed is False
    assert decision.status == "UNKNOWN"


def test_불리언_범주_표기는_단위_비교_대상이_아니다() -> None:
    rule = _rule(
        field_name="active_pregnancy",
        operator="=",
        value_low="false",
        value_high=None,
        unit="boolean",
        kind=CriterionKind.DERIVED_BOOLEAN,
    )
    bundle = _bundle(rule=rule, value=False, unit="boolean")
    _, report = _confirm(bundle, _judgment("OK"))
    assert _check(report, "V-UNIT").passed is True


def test_규칙_계산을_뒤집으면_연산자_검증이_실패한다() -> None:
    _, report = _confirm(_bundle(satisfied=True), _judgment("NOT_OK"))
    assert _check(report, "V-OPERATOR").passed is False


def test_제외_기준_방향을_뒤집으면_유형_검증이_실패한다() -> None:
    """제외 기준에 해당하는 근거가 있는데 충족으로 판단하면 잡아낸다."""
    rule = _rule(
        criterion_type="EXCLUSION",
        field_name="active_pregnancy",
        operator="=",
        value_low="false",
        value_high=None,
        unit="boolean",
        kind=CriterionKind.DERIVED_BOOLEAN,
    )
    bundle = _bundle(rule=rule, value=True, unit="boolean", satisfied=False)
    decision, report = _confirm(
        bundle,
        _judgment("OK"),
        _rule_verification(CriterionStatus.CONTRADICTED),
    )
    assert _check(report, "V-TYPE").passed is False
    assert decision.route in {A2A, HUMAN_REVIEW}
    assert decision.status == "UNKNOWN"


@pytest.mark.parametrize(
    "reason",
    [
        "홍길동 님의 최근 기록을 확인했습니다.",
        "연락처 010-1234-5678 로 확인했습니다.",
        "person_id=12345 기록을 참조했습니다.",
        "주민번호 900101-1234567 확인.",
    ],
)
def test_직접_식별자가_섞인_판단은_확정하지_못한다(reason: str) -> None:
    decision, report = _confirm(_bundle(), _judgment("OK", reason=reason))
    assert _check(report, "V-PII").passed is False
    assert decision.status == "UNKNOWN"
    assert decision.route == HUMAN_REVIEW


def test_가명_키와_출처_ID는_식별자로_보지_않는다() -> None:
    _, report = _confirm(
        _bundle(),
        _judgment("OK", reason="pt_7f3a 의 ENC-1 기록에서 확인했습니다."),
    )
    assert _check(report, "V-PII").passed is True


def test_낮은_확신도로는_확정하지_못한다() -> None:
    decision, report = _confirm(_bundle(), _judgment("OK", confidence=0.3))
    assert _check(report, "V-CONFIDENCE").passed is False
    assert decision.status == "UNKNOWN"


# ---------------------------------------------------------------------------
# Rule Aggregator: 최종 상태 확정 규칙 표
# ---------------------------------------------------------------------------


def test_규칙과_판단이_일치하면_OK로_확정한다() -> None:
    decision, _ = _confirm(_bundle(satisfied=True), _judgment("OK"))
    assert decision.status == "OK"
    assert decision.route == DECIDED
    assert decision.criterion_status is CriterionStatus.EVIDENCE_FOUND


def test_규칙과_판단이_일치하면_NOT_OK로_확정한다() -> None:
    decision, _ = _confirm(
        _bundle(satisfied=False),
        _judgment("NOT_OK"),
        _rule_verification(CriterionStatus.CONTRADICTED),
    )
    assert decision.status == "NOT_OK"
    assert decision.route == DECIDED
    assert decision.criterion_status is CriterionStatus.CONTRADICTED


def test_판단과_규칙이_충돌하면_A2A로_넘긴다() -> None:
    decision, _ = _confirm(
        _bundle(satisfied=True),
        _judgment("NOT_OK"),
        _rule_verification(CriterionStatus.EVIDENCE_FOUND),
    )
    assert decision.status == "UNKNOWN"
    assert decision.route == A2A
    assert decision.criterion_status is CriterionStatus.CONFLICTING
    assert decision.needs_deliberation is True
    assert any("충돌" in item for item in decision.verification.conflicts)


def test_제외_기준의_근거_부족은_사람_검토로_넘긴다() -> None:
    """놓친 제외 기준은 되돌릴 수 없으므로 질문으로 미루지 않는다."""
    rule = _rule(criterion_type="EXCLUSION", time_window_days=180)
    bundle = _bundle(rule=rule, observed_at="2020-01-01")
    decision, _ = _confirm(bundle, _judgment("OK"))
    assert decision.route == HUMAN_REVIEW
    assert decision.criterion_status is CriterionStatus.REVIEW_REQUIRED


def test_선정_기준의_근거_부족은_정보_부족으로_남긴다() -> None:
    bundle = _bundle(rule=_rule(time_window_days=180), observed_at="2020-01-01")
    decision, _ = _confirm(bundle, _judgment("OK"))
    assert decision.route == DECIDED
    assert decision.criterion_status is CriterionStatus.UNKNOWN


def test_규칙이_판정했는데_모델이_보류하면_규칙을_유지한다() -> None:
    """확신도만 낮추고 상태는 규칙 판정을 따른다."""
    decision, _ = _confirm(
        _bundle(satisfied=True),
        _judgment("UNKNOWN", evidence=(), missing=("최근 검사일",)),
    )
    assert decision.status == "OK"
    assert decision.criterion_status is CriterionStatus.EVIDENCE_FOUND
    assert decision.verification.confidence <= 0.7
    assert any("최근 검사일" in note for note in decision.verification.notes)


def test_규칙이_판정하지_못하면_모델_제안만으로_올리지_않는다() -> None:
    """자유서술만 있는 근거로 모델이 충족을 주장해도 상태를 올리지 않는다."""
    bundle = _bundle(satisfied=None, value=None, narrative=True)
    decision, report = _confirm(
        bundle,
        _judgment("OK", evidence=("NOTE-ENC-1",)),
        _rule_verification(CriterionStatus.REVIEW_REQUIRED, confidence=0.9),
    )
    assert report.passed is True, "검증은 통과했지만 규칙이 판정하지 못한 경우다."
    assert decision.status == "UNKNOWN"
    assert decision.route == A2A
    assert decision.criterion_status is CriterionStatus.REVIEW_REQUIRED


def test_확정적_주장에_출처가_없으면_사람_검토로_넘긴다() -> None:
    decision, _ = _confirm(
        _bundle(satisfied=None, value=None),
        _judgment("OK", evidence=()),
        _rule_verification(CriterionStatus.UNKNOWN, confidence=0.0),
    )
    assert decision.status == "UNKNOWN"
    assert decision.route == HUMAN_REVIEW


def test_규칙의_검토_필요_상태는_보존된다() -> None:
    """참여자 진술 기반 값처럼 규칙이 검토로 올린 상태를 모델이 덮지 못한다."""
    decision, _ = _confirm(
        _bundle(satisfied=True),
        _judgment("UNKNOWN", evidence=()),
        _rule_verification(CriterionStatus.REVIEW_REQUIRED, confidence=0.5),
    )
    assert decision.status == "UNKNOWN"
    assert decision.criterion_status is CriterionStatus.REVIEW_REQUIRED


def test_규칙의_기록_충돌_상태는_보존된다() -> None:
    decision, _ = _confirm(
        _bundle(satisfied=True),
        _judgment("UNKNOWN", evidence=()),
        _rule_verification(
            CriterionStatus.CONFLICTING,
            confidence=0.4,
            conflicts=("구조화 기록과 자유서술이 어긋납니다.",),
        ),
    )
    assert decision.criterion_status is CriterionStatus.CONFLICTING
    assert decision.verification.conflicts


def test_판단_요약은_감사에_필요한_필드를_갖는다() -> None:
    decision, _ = _confirm(_bundle(), _judgment("OK"))
    payload = decision.to_dict()
    assert payload["status"] == "OK"
    assert payload["route"] == DECIDED
    assert payload["rule_status"] == "EVIDENCE_FOUND"
    assert payload["judgment"]["used_evidence_ids"] == ["ENC-1"]
    assert payload["verification"]["passed"] is True
    assert len(payload["verification"]["checks"]) == 7


# ---------------------------------------------------------------------------
# 규칙 승계 경로는 규칙 판정을 바꾸지 않는다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        CriterionStatus.EVIDENCE_FOUND,
        CriterionStatus.CONTRADICTED,
        CriterionStatus.UNKNOWN,
        CriterionStatus.CONFLICTING,
        CriterionStatus.REVIEW_REQUIRED,
    ],
)
def test_모델이_없으면_규칙_판정이_그대로_유지된다(status: CriterionStatus) -> None:
    """판단 단계를 켜도 모델이 없으면 상태가 바뀌지 않아야 한다."""
    bundle = _bundle(
        satisfied={
            CriterionStatus.EVIDENCE_FOUND: True,
            CriterionStatus.CONTRADICTED: False,
        }.get(status)
    )
    verification = _rule_verification(
        status,
        confidence=0.9
        if status
        in (CriterionStatus.EVIDENCE_FOUND, CriterionStatus.CONTRADICTED)
        else 0.4,
    )
    judgment = _judge(None).judge(bundle, rule_verification=verification)
    decision, _ = _confirm(bundle, judgment, verification)
    assert decision.criterion_status is status


def test_로컬_검증기_판정이_판단_단계를_지나도_같다() -> None:
    """실제 규칙 검증기 출력으로 확인한다."""
    bundle = _bundle(satisfied=True)
    verification = LocalEvidenceVerifier().verify(bundle)
    judgment = _judge(None).judge(bundle, rule_verification=verification)
    decision, report = _confirm(bundle, judgment, verification)
    assert report.passed is True
    assert decision.criterion_status is verification.proposed_status
    assert decision.verification.confidence == verification.confidence


# ---------------------------------------------------------------------------
# 실행 종단: 오케스트레이터 배선
# ---------------------------------------------------------------------------


def _run(model_client=None):
    from app.config import settings
    from app.container import build_container

    container = build_container(settings.data_dir, model_client=model_client)
    person_id = sorted(container.repository.patients)[0]
    output = container.orchestrator.run(
        person_id=person_id, trial_id="SYN-T2D-INTENSIFY-01"
    )
    return container, output


def test_실행_결과에_판단_요약이_남는다() -> None:
    _, output = _run()
    summary = output.run.metadata["judgment"]

    assert summary["enabled"] is True
    assert summary["criteria_total"] == output.outcome.criteria_total
    assert sum(summary["decisions"].values()) == summary["criteria_total"]
    assert set(summary["decisions"]) == {"OK", "NOT_OK", "UNKNOWN"}
    assert all(len(item["verification"]["checks"]) == 7 for item in summary["items"])


def test_판단_상태와_기준_상태가_어긋나지_않는다() -> None:
    """3값 상태와 내부 5값 상태의 대응이 유지되는지 확인한다."""
    _, output = _run()
    by_id = {item["criterion_id"]: item for item in output.run.metadata["judgment"]["items"]}

    for result in output.run.results:
        decision = by_id[result.criterion_id]
        assert decision["criterion_status"] == str(result.status)
        if decision["status"] == "OK":
            assert result.status is CriterionStatus.EVIDENCE_FOUND
        elif decision["status"] == "NOT_OK":
            assert result.status is CriterionStatus.CONTRADICTED
        else:
            assert result.status is not CriterionStatus.EVIDENCE_FOUND


def test_판단_단계가_감사_로그에_남는다() -> None:
    container, output = _run()
    events = container.audit.for_run(output.run.run_id)
    judged = [item for item in events if item["action"] == "CRITERION_JUDGED"]

    assert len(judged) == output.outcome.criteria_total
    detail = judged[0]["detail"]
    assert detail["proposed_status"] in {"OK", "NOT_OK", "UNKNOWN"}
    assert detail["route"] in {DECIDED, A2A, HUMAN_REVIEW}
    assert detail["fabricated_evidence_ids"] == []


def test_모델을_붙여도_판정은_결정론적_경로와_같다() -> None:
    """스텁 모델이 판단해도 확정 상태는 규칙 경로와 같아야 한다."""
    from agent.model import StubModelClient

    _, plain = _run()
    _, agentic = _run(StubModelClient())

    assert [item.status for item in plain.run.results] == [
        item.status for item in agentic.run.results
    ]
    assert agentic.run.metadata["judgment"]["mode"] == "model"
    assert plain.run.metadata["judgment"]["mode"] == "rule"
    # 근거가 있는 기준은 모델이 판단하고, 근거가 없으면 규칙을 승계한다.
    origins = {
        item["judgment"]["origin"]
        for item in agentic.run.metadata["judgment"]["items"]
    }
    assert "model" in origins


def test_판단_단계를_끄면_NLI_검증기_경로를_쓴다() -> None:
    from agent.model import StubModelClient
    from app.config import ModelSettings, settings
    from app.container import build_container

    container = build_container(
        settings.data_dir,
        model_config=ModelSettings(enabled=True, criterion_judge_enabled=False),
        model_client=StubModelClient(),
    )
    person_id = sorted(container.repository.patients)[0]
    output = container.orchestrator.run(
        person_id=person_id, trial_id="SYN-T2D-INTENSIFY-01"
    )

    assert container.orchestrator._judge is None
    assert output.run.metadata["judgment"] == {"enabled": False, "items": []}
    names = {
        span["name"]
        for span in container.trace.spans_for(output.run.run_id)
        if span["kind"] == "MODEL"
    }
    assert "model:nli_verifier" in names
    assert "model:criterion_judge" not in names
