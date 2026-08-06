"""6계층 Evidence Bundle 과 그 구성 요소.

Tool 이 만든 부분 근거를 하나의 구조체로 묶어 Verifier 로 넘기고,
Aggregator 가 그 결과를 상태로 환원한다. 모든 근거는 출처 ID를 갖는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .states import CriterionKind, CriterionStatus

EvidenceSource = Literal[
    "CRITERIA_STORE",
    "TIMELINE_GRAPH",
    "NARRATIVE_EMR",
    "RULE_ENGINE",
    "PATIENT_REPORTED",
    "APPLICATION",
]
"""근거의 출처.

`PATIENT_REPORTED` 는 참여자·연구자가 제출한 자유 문장에서 Intake 가 정규화한 값이다.
기록으로 확인된 값과 같은 무게로 다루지 않는다. Verifier 가 이 출처를 보고
판정을 확정하지 않고 검토로 올린다.
"""

PATIENT_REPORTED: EvidenceSource = "PATIENT_REPORTED"
APPLICATION: EvidenceSource = "APPLICATION"


@dataclass(frozen=True)
class CriterionRule:
    """Criteria Tool 이 반환하는 실행 규칙 한 건."""

    criterion_id: str
    criterion_type: Literal["INCLUSION", "EXCLUSION"]
    field_name: str
    operator: str
    value_low: str | None
    value_high: str | None
    unit: str | None
    label: str
    kind: CriterionKind
    trial_id: str
    criteria_version: str
    time_window_days: int | None = None
    """기준이 요구하는 관찰 기간. 없으면 시점 제약이 없는 기준이다.

    공고 기준 JSON 의 `time_window_days` 에 대응한다. 값이 있으면 관찰 시점이
    인덱스 방문에서 이 일수 안에 들어와야 근거로 인정한다. 판정은 규칙이 하고,
    기간 위반 여부는 Judgment Verifier 가 검사한다.
    """

    def expected_repr(self) -> str:
        """기준 조건을 사람이 읽을 수 있는 문자열로 만든다."""
        if self.operator == "between":
            return f"{self.value_low}-{self.value_high}"
        if self.value_low is None:
            return self.operator
        return f"{self.operator}{self.value_low}"


@dataclass(frozen=True)
class Observation:
    """관찰값 한 건. 출처와 측정 시점을 반드시 동반한다."""

    field_name: str
    value: float | str | bool | None
    unit: str | None
    observed_at: str | None
    source: EvidenceSource
    source_id: str
    detail: str | None = None


@dataclass(frozen=True)
class NarrativeSnippet:
    """검색된 원문 근거 문장.

    ``document_type``으로 환자 임상기록과 공개 공고·표준문서를 구분한다. 공개
    참고문서는 지원자의 사실값으로 해석하지 않고 기준의 출처·설명에만 사용한다.
    """

    note_id: str
    encounter_id: str
    note_date: str
    snippet: str
    matched_terms: tuple[str, ...]
    score: float
    document_type: str = "patient_evidence"
    source_title: str | None = None


@dataclass(frozen=True)
class RuleOutcome:
    """Rule Evaluator 의 결정론적 계산 결과."""

    criterion_id: str
    satisfied: bool | None
    observed_repr: str | None
    expected_repr: str
    explanation: str


@dataclass
class EvidenceBundle:
    """조건 · 관찰값 · 날짜 · 원문 · 출처 ID 를 묶은 검증 입력 단위."""

    rule: CriterionRule
    observations: list[Observation] = field(default_factory=list)
    narrative: list[NarrativeSnippet] = field(default_factory=list)
    outcome: RuleOutcome | None = None
    tool_calls: list[str] = field(default_factory=list)

    @property
    def primary_observation(self) -> Observation | None:
        """규칙 판정에 사용한 대표 관찰값."""
        for item in self.observations:
            if item.field_name == self.rule.field_name and item.value is not None:
                return item
        return None

    def source_ids(self) -> list[str]:
        """이 번들이 참조한 모든 출처 ID."""
        ids = [item.source_id for item in self.observations]
        ids.extend(item.note_id for item in self.narrative)
        return ids


@dataclass(frozen=True)
class Verification:
    """Evidence Verifier 판정. status 는 Aggregator 가 최종 확정한다."""

    criterion_id: str
    proposed_status: CriterionStatus
    confidence: float
    grounded: bool
    conflicts: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CriterionResult:
    """Aggregator 가 확정한 기준별 최종 결과."""

    criterion_id: str
    criterion_type: str
    label: str
    field_name: str
    kind: CriterionKind
    status: CriterionStatus
    observed_value: str | None
    expected_condition: str
    unit: str | None
    observed_at: str | None
    confidence: float
    explanation: str
    source_ids: tuple[str, ...]
    narrative: tuple[NarrativeSnippet, ...] = ()
    conflicts: tuple[str, ...] = ()
    rule_satisfied: bool | None = None
    patient_reported: bool = False

    def is_blocking(self) -> bool:
        """이 결과가 종합 판정을 부적합으로 확정시키는지 여부."""
        return self.status is CriterionStatus.CONTRADICTED


@dataclass
class ScreeningRun:
    """9계층에 저장되는 실행 레코드."""

    run_id: str
    person_id: int | None
    trial_id: str
    criteria_version: str
    index_encounter_id: str
    index_date: str
    started_at: str
    finished_at: str | None = None
    eligibility_status: str | None = None
    results: list[CriterionResult] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
