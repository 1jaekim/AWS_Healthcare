"""가입 설문 관심 분야가 추천 순서에 개입하는 범위를 고정한다.

관심 분야는 동점을 가르는 데만 쓴다. 판정 결과·적합도 점수·사람 확인 필요 여부가
모두 같을 때에만 순서가 바뀌고, 후보를 걸러내지는 않는다. 관심 목록에 없다고
빼버리면 실제로 적격인 공고가 사용자에게 보이지 않는데, 임상시험 매칭에서는 그
방향의 실수가 더 나쁘다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.actions.recommendation import TrialRanker
from app.domain.states import CriterionStatus


@dataclass
class _Result:
    criterion_id: str
    status: CriterionStatus
    criterion_type: str = "INCLUSION"
    label: str = "기준"
    explanation: str = "이유"
    source_ids: tuple[str, ...] = ()
    patient_reported: bool = False
    rule_satisfied: bool | None = None


@dataclass
class _Outcome:
    screening_decision: str
    criteria_met: int
    criteria_total: int


@dataclass
class _Run:
    trial_id: str
    run_id: str
    results: list[_Result]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Output:
    run: _Run
    outcome: _Outcome
    requests: list[dict] = field(default_factory=list)
    human_review_criteria: list[str] = field(default_factory=list)


def _output(trial_id: str, *, status: CriterionStatus, count: int = 3) -> _Output:
    results = [
        _Result(criterion_id=f"{trial_id}-C{index}", status=status)
        for index in range(count)
    ]
    decision = {
        CriterionStatus.EVIDENCE_FOUND: "OK",
        CriterionStatus.CONTRADICTED: "NOT_OK",
    }.get(status, "UNKNOWN")
    return _Output(
        run=_Run(trial_id=trial_id, run_id=f"RUN-{trial_id}", results=results),
        outcome=_Outcome(
            screening_decision=decision, criteria_met=count, criteria_total=count
        ),
    )


CATALOG = {
    "T-SKIN": {"trial_name": "아토피 피부염 연구", "description": "피부 질환"},
    "T-DIAB": {"trial_name": "2형 당뇨 연구", "description": "당뇨 및 내분비"},
    "T-CARD": {"trial_name": "심혈관 연구", "description": "고혈압"},
}


def test_interest_area_breaks_ties_only() -> None:
    """모든 조건이 같으면 관심 분야가 앞선다."""
    outputs = [
        _output("T-CARD", status=CriterionStatus.EVIDENCE_FOUND),
        _output("T-DIAB", status=CriterionStatus.EVIDENCE_FOUND),
        _output("T-SKIN", status=CriterionStatus.EVIDENCE_FOUND),
    ]
    ranker = TrialRanker()

    # 관심 분야 없음 → trial_id 알파벳순
    plain, _ = ranker.rank(outputs, trial_catalog=CATALOG, top_k=5)
    assert [item["trial_id"] for item in plain] == ["T-CARD", "T-DIAB", "T-SKIN"]

    # 피부 관심 → 동점 안에서만 앞으로
    skin, _ = ranker.rank(
        outputs, trial_catalog=CATALOG, top_k=5, interest_areas=("피부",)
    )
    assert [item["trial_id"] for item in skin] == ["T-SKIN", "T-CARD", "T-DIAB"]

    diabetes, _ = ranker.rank(
        outputs, trial_catalog=CATALOG, top_k=5, interest_areas=("당뇨 · 내분비",)
    )
    assert [item["trial_id"] for item in diabetes] == ["T-DIAB", "T-CARD", "T-SKIN"]


def test_interest_area_never_outranks_eligibility() -> None:
    """관심 분야가 적격 판정을 앞지르지 못한다."""
    outputs = [
        # 관심 분야와 맞지만 판정은 UNKNOWN
        _output("T-SKIN", status=CriterionStatus.UNKNOWN),
        # 관심 분야와 안 맞지만 판정은 OK
        _output("T-CARD", status=CriterionStatus.EVIDENCE_FOUND),
    ]
    recommended, _ = TrialRanker().rank(
        outputs, trial_catalog=CATALOG, top_k=5, interest_areas=("피부",)
    )
    assert [item["trial_id"] for item in recommended] == ["T-CARD", "T-SKIN"]


def test_interest_area_does_not_filter_candidates() -> None:
    """관심 분야에 없는 공고도 목록에 남는다."""
    outputs = [
        _output("T-CARD", status=CriterionStatus.EVIDENCE_FOUND),
        _output("T-DIAB", status=CriterionStatus.EVIDENCE_FOUND),
    ]
    recommended, excluded = TrialRanker().rank(
        outputs, trial_catalog=CATALOG, top_k=5, interest_areas=("피부",)
    )
    assert {item["trial_id"] for item in recommended} == {"T-CARD", "T-DIAB"}
    assert excluded == []


def test_excluded_trials_stay_excluded_regardless_of_interest() -> None:
    """확정 부적합은 관심 분야와 무관하게 제외된다."""
    outputs = [
        _output("T-SKIN", status=CriterionStatus.CONTRADICTED),
        _output("T-CARD", status=CriterionStatus.EVIDENCE_FOUND),
    ]
    recommended, excluded = TrialRanker().rank(
        outputs, trial_catalog=CATALOG, top_k=5, interest_areas=("피부",)
    )
    assert [item["trial_id"] for item in recommended] == ["T-CARD"]
    assert [item["trial_id"] for item in excluded] == ["T-SKIN"]


def test_interest_match_is_reported() -> None:
    """왜 이 순서인지 확인할 수 있게 겹친 단어 수를 노출한다."""
    outputs = [_output("T-DIAB", status=CriterionStatus.EVIDENCE_FOUND)]
    recommended, _ = TrialRanker().rank(
        outputs,
        trial_catalog=CATALOG,
        top_k=5,
        interest_areas=("당뇨 · 내분비",),
    )
    # `당뇨` 와 `내분비` 두 단어가 모두 공고 설명에 있다.
    assert recommended[0]["interest_match"] == 2
