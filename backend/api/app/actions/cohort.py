"""Cohort Selector: 후보군 퍼널, 모집 병목, 검토 우선순위를 산출한다.

여러 환자의 실행 결과를 모아 시험 단위로 집계한다. 병목은 '어떤 기준에서 판정이
막히는가' 를 기준별 미해소·충돌 건수로 표현한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain.models import ScreeningRun
from ..domain.states import CriterionStatus, EligibilityStatus

_FUNNEL_ORDER: tuple[EligibilityStatus, ...] = (
    EligibilityStatus.ELIGIBLE,
    EligibilityStatus.REVIEW_REQUIRED,
    EligibilityStatus.NEEDS_MORE_EVIDENCE,
    EligibilityStatus.INELIGIBLE,
)

_FUNNEL_LABELS: dict[EligibilityStatus, str] = {
    EligibilityStatus.ELIGIBLE: "사전 적합",
    EligibilityStatus.REVIEW_REQUIRED: "검토 필요",
    EligibilityStatus.NEEDS_MORE_EVIDENCE: "추가 근거 필요",
    EligibilityStatus.INELIGIBLE: "사전 부적합",
}


@dataclass
class Bottleneck:
    """기준 하나의 병목 지표."""

    criterion_id: str
    label: str
    field_name: str
    contradicted: int = 0
    unknown: int = 0
    conflicting: int = 0
    review_required: int = 0
    evaluated: int = 0

    @property
    def unresolved(self) -> int:
        return self.unknown + self.conflicting + self.review_required

    @property
    def block_rate(self) -> float:
        if self.evaluated == 0:
            return 0.0
        return round((self.contradicted + self.unresolved) / self.evaluated, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "label": self.label,
            "field": self.field_name,
            "evaluated": self.evaluated,
            "contradicted": self.contradicted,
            "unknown": self.unknown,
            "conflicting": self.conflicting,
            "review_required": self.review_required,
            "unresolved": self.unresolved,
            "block_rate": self.block_rate,
        }


@dataclass
class CohortView:
    """시험 단위 코호트 요약."""

    trial_id: str
    total_screened: int
    funnel: list[dict[str, Any]] = field(default_factory=list)
    bottlenecks: list[dict[str, Any]] = field(default_factory=list)
    review_priority: list[dict[str, Any]] = field(default_factory=list)


class CohortSelector:
    """실행 결과 집합에서 코호트 뷰를 만든다."""

    def build(self, trial_id: str, runs: list[ScreeningRun]) -> CohortView:
        counts: dict[str, int] = {}
        bottlenecks: dict[str, Bottleneck] = {}

        for run in runs:
            if run.eligibility_status:
                counts[run.eligibility_status] = (
                    counts.get(run.eligibility_status, 0) + 1
                )
            for result in run.results:
                entry = bottlenecks.setdefault(
                    result.criterion_id,
                    Bottleneck(
                        criterion_id=result.criterion_id,
                        label=result.label,
                        field_name=result.field_name,
                    ),
                )
                entry.evaluated += 1
                if result.status is CriterionStatus.CONTRADICTED:
                    entry.contradicted += 1
                elif result.status is CriterionStatus.UNKNOWN:
                    entry.unknown += 1
                elif result.status is CriterionStatus.CONFLICTING:
                    entry.conflicting += 1
                elif result.status is CriterionStatus.REVIEW_REQUIRED:
                    entry.review_required += 1

        total = len(runs)
        funnel = [
            {
                "status": str(status),
                "label": _FUNNEL_LABELS[status],
                "count": counts.get(str(status), 0),
                "share": round(counts.get(str(status), 0) / total, 3) if total else 0.0,
            }
            for status in _FUNNEL_ORDER
        ]

        ranked = sorted(
            bottlenecks.values(),
            key=lambda item: (-item.unresolved, -item.block_rate, item.criterion_id),
        )

        return CohortView(
            trial_id=trial_id,
            total_screened=total,
            funnel=funnel,
            bottlenecks=[item.to_dict() for item in ranked],
            review_priority=self._review_priority(runs),
        )

    @staticmethod
    def _review_priority(runs: list[ScreeningRun]) -> list[dict[str, Any]]:
        """검토 우선순위. 해소 가능성이 높은 순으로 정렬한다."""
        candidates: list[dict[str, Any]] = []
        for run in runs:
            unresolved = [
                item
                for item in run.results
                if item.status
                in (
                    CriterionStatus.UNKNOWN,
                    CriterionStatus.CONFLICTING,
                    CriterionStatus.REVIEW_REQUIRED,
                )
            ]
            if not unresolved:
                continue
            if run.eligibility_status == str(EligibilityStatus.INELIGIBLE):
                continue
            met = sum(
                1
                for item in run.results
                if item.status is CriterionStatus.EVIDENCE_FOUND
            )
            total = len(run.results) or 1
            candidates.append(
                {
                    "run_id": run.run_id,
                    "person_id": run.person_id,
                    "eligibility_status": run.eligibility_status,
                    "unresolved_count": len(unresolved),
                    "criteria_met": met,
                    "criteria_total": total,
                    "completion": round(met / total, 3),
                    "unresolved_criteria": [
                        item.criterion_id for item in unresolved
                    ],
                }
            )
        candidates.sort(
            key=lambda item: (-item["completion"], item["unresolved_count"])
        )
        return candidates
