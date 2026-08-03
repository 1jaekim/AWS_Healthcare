"""Evidence Packet: 기준별 근거·날짜·출처·불확실성을 포장한다.

관리자 화면의 근거 표시와 감사 추적이 같은 패킷을 참조하도록 단일 구조로 만든다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain.models import CriterionResult
from ..domain.states import CriterionStatus
from ..reasoning.aggregator import AggregateOutcome

_STATUS_LABELS: dict[CriterionStatus, str] = {
    CriterionStatus.EVIDENCE_FOUND: "근거 확인",
    CriterionStatus.CONTRADICTED: "조건 충돌",
    CriterionStatus.UNKNOWN: "정보 부족",
    CriterionStatus.CONFLICTING: "기록 간 충돌",
    CriterionStatus.REVIEW_REQUIRED: "검토 필요",
}

_CONFIDENCE_BANDS: tuple[tuple[float, str], ...] = (
    (0.8, "높음"),
    (0.55, "중간"),
    (0.0, "낮음"),
)


def confidence_band(score: float) -> str:
    """신뢰도 점수를 화면 표기용 구간으로 변환한다."""
    for threshold, label in _CONFIDENCE_BANDS:
        if score >= threshold:
            return label
    return "낮음"


@dataclass
class EvidencePacket:
    """실행 한 건의 근거 패킷."""

    run_id: str
    person_id: int
    trial_id: str
    criteria_version: str
    index_encounter_id: str
    index_date: str
    eligibility_status: str
    decision_label: str
    criteria_total: int
    criteria_met: int
    items: list[dict[str, Any]] = field(default_factory=list)
    uncertainty: dict[str, Any] = field(default_factory=dict)


class EvidencePacketBuilder:
    """CriterionResult 목록을 화면·감사 공용 패킷으로 만든다."""

    def build(
        self,
        *,
        run_id: str,
        person_id: int,
        trial_id: str,
        criteria_version: str,
        index_encounter_id: str,
        index_date: str,
        results: list[CriterionResult],
        outcome: AggregateOutcome,
    ) -> EvidencePacket:
        items = [self._item(result) for result in results]
        return EvidencePacket(
            run_id=run_id,
            person_id=person_id,
            trial_id=trial_id,
            criteria_version=criteria_version,
            index_encounter_id=index_encounter_id,
            index_date=index_date,
            eligibility_status=str(outcome.eligibility_status),
            decision_label=outcome.decision_label,
            criteria_total=outcome.criteria_total,
            criteria_met=outcome.criteria_met,
            items=items,
            uncertainty={
                "blocking_criteria": list(outcome.blocking_criteria),
                "open_criteria": list(outcome.open_criteria),
                "review_criteria": list(outcome.review_criteria),
                "unresolved_count": len(outcome.open_criteria)
                + len(outcome.review_criteria),
            },
        )

    def _item(self, result: CriterionResult) -> dict[str, Any]:
        """기준 한 건의 표시용 항목. 출처 ID와 원문 문장을 함께 싣는다."""
        return {
            "criterion_id": result.criterion_id,
            "criterion_type": result.criterion_type,
            "label": result.label,
            "field": result.field_name,
            "kind": str(result.kind),
            "status": str(result.status),
            "status_label": _STATUS_LABELS[result.status],
            "observed_value": result.observed_value,
            "expected_condition": result.expected_condition,
            "unit": result.unit,
            "observed_at": result.observed_at,
            "confidence": result.confidence,
            "confidence_band": confidence_band(result.confidence),
            "explanation": result.explanation,
            "source_ids": list(result.source_ids),
            "conflicts": list(result.conflicts),
            "narrative": [
                {
                    "note_id": snippet.note_id,
                    "encounter_id": snippet.encounter_id,
                    "note_date": snippet.note_date,
                    "snippet": snippet.snippet,
                    "matched_terms": list(snippet.matched_terms),
                    "score": snippet.score,
                }
                for snippet in result.narrative
            ],
        }
