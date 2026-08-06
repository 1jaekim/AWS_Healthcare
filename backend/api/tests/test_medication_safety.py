"""식약처 DUR 관계가 공고의 명시적 약물 제외조건에만 적용되는지 검증한다."""

from __future__ import annotations

from app.domain.models import CriterionResult, CriterionRule
from app.domain.states import CriterionKind, CriterionStatus
from app.reasoning.medication_safety import (
    MedicationSafetyEvaluator,
    load_diabetes_contraindications,
)


def _rule(
    label: str,
    *,
    criterion_type: str = "EXCLUSION",
    field_name: str = "current_medications",
) -> CriterionRule:
    return CriterionRule(
        criterion_id="MED-C01",
        criterion_type=criterion_type,  # type: ignore[arg-type]
        field_name=field_name,
        operator="=",
        value_low="false",
        value_high=None,
        unit=None,
        label=label,
        kind=CriterionKind.NARRATIVE,
        trial_id="TRIAL-1",
        criteria_version="v1",
    )


def _result(rule: CriterionRule) -> CriterionResult:
    return CriterionResult(
        criterion_id=rule.criterion_id,
        criterion_type=rule.criterion_type,
        label=rule.label,
        field_name=rule.field_name,
        kind=rule.kind,
        status=CriterionStatus.UNKNOWN,
        observed_value=None,
        expected_condition=rule.expected_repr(),
        unit=None,
        observed_at=None,
        confidence=0.0,
        explanation="판정 근거를 확인할 수 없습니다.",
        source_ids=(),
    )


def test_diabetes_snapshot_contains_exactly_ten_relations() -> None:
    relations = load_diabetes_contraindications()
    assert len(relations) == 10
    assert any(
        item.ingredient_name_ko == "이오파미돌"
        and item.related_ingredient_name_ko == "메트포르민"
        and item.reason == "급성신부전"
        for item in relations
    )


def test_explicit_general_contraindication_blocks_known_current_pair() -> None:
    rule = _rule("식약처 DUR 병용금기 약물 조합을 복용 중인 지원자는 제외")
    results, summary = MedicationSafetyEvaluator().apply(
        rules=[rule],
        results=[_result(rule)],
        current_medications=("메트포르민 500mg", "이오파미돌"),
        application_id="APP-1",
    )

    assert results[0].status is CriterionStatus.CONTRADICTED
    assert results[0].observed_value == "이오파미돌–메트포르민"
    assert "급성신부전" in results[0].explanation
    assert "APP-1:current_medications" in results[0].source_ids
    assert "standard:mfds-dur:coadministration:diabetes-10" in results[0].source_ids
    assert summary["applied"][0]["relation_id"] == "D000267-D000718"


def test_specific_counterpart_in_trial_rule_blocks_current_medication() -> None:
    rule = _rule("이오파미돌 투여 예정자는 메트포르민 복용 시 제외")
    results, _ = MedicationSafetyEvaluator().apply(
        rules=[rule],
        results=[_result(rule)],
        current_medications=("Metformin hydrochloride extended release",),
        application_id="APP-2",
    )
    assert results[0].status is CriterionStatus.CONTRADICTED


def test_dur_pair_does_not_block_trial_without_medication_exclusion() -> None:
    rule = _rule(
        "현재 임신 중인 지원자는 제외",
        field_name="active_pregnancy",
    )
    original = _result(rule)
    results, summary = MedicationSafetyEvaluator().apply(
        rules=[rule],
        results=[original],
        current_medications=("메트포르민", "이오파미돌"),
        application_id="APP-3",
    )
    assert results == [original]
    assert summary["applied"] == []


def test_inclusion_medication_text_never_becomes_dur_exclusion() -> None:
    rule = _rule(
        "메트포르민과 이오파미돌 복용 정보 수집",
        criterion_type="INCLUSION",
    )
    original = _result(rule)
    results, summary = MedicationSafetyEvaluator().apply(
        rules=[rule],
        results=[original],
        current_medications=("메트포르민", "이오파미돌"),
        application_id="APP-4",
    )
    assert results == [original]
    assert summary["applied"] == []
